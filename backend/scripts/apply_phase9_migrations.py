"""Check/apply/verify recommendation v1 migrations with an execution ledger."""
from __future__ import annotations

import argparse
import hashlib
import os
import time
from pathlib import Path

from sqlalchemy import create_engine, text


LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migration_history (
  migration_name VARCHAR(255) NOT NULL PRIMARY KEY,
  sha256 CHAR(64) NOT NULL,
  applied_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  applied_by VARCHAR(128) NOT NULL,
  duration_ms INT UNSIGNED NOT NULL DEFAULT 0,
  success TINYINT(1) NOT NULL,
  server_version VARCHAR(128) NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# Upsert instead of plain INSERT: a previous run may have recorded success=0 for
# this migration, and re-applying it must overwrite that row.  The ledger column
# semantics stay exactly as documented in §11.1.1 -- failure detail is printed,
# never smuggled into an existing column.
LEDGER_UPSERT = (
    "INSERT INTO schema_migration_history "
    "(migration_name, sha256, applied_at, applied_by, duration_ms, success, server_version) "
    "VALUES (:name, :digest, CURRENT_TIMESTAMP(6), :applied_by, :duration, :success, :server_version) "
    "ON DUPLICATE KEY UPDATE "
    "sha256 = :digest, "
    "applied_at = CURRENT_TIMESTAMP(6), "
    "applied_by = :applied_by, "
    "duration_ms = :duration, "
    "success = :success, "
    "server_version = :server_version"
)

MIN_MYSQL_VERSION = (8, 0)

# --verify structural expectations.  The ledger alone cannot tell whether the
# DDL really landed: a half-applied file, a manual rollback or a restored
# pre-phase9 backup all leave the ledger intact.
REQUIRED_TABLES = (
    "recommendation_strategy_version",
    "recommendation_strategy_release",
    "recommendation_release_history",
    "recommendation_runtime_control",
    "recommendation_request",
    "recommendation_search_attempt",
    "recommendation_delivery",
    "recommendation_impression",
    "recommendation_exposure_daily",
)

REQUIRED_COLUMNS = (
    ("admin_user", "role"),
    ("recommendation_delivery", "impression_lease_owner"),
    ("recommendation_delivery", "invalid_recipients"),
    ("recommendation_search_attempt", "llm_retry_count"),
    ("event_log", "attribution_dedupe_key"),
)

REQUIRED_INDEXES = (
    ("recommendation_impression", "uk_recommendation_impression_delivery_target"),
    ("recommendation_delivery", "idx_recommendation_delivery_impression_lease"),
)


def parse_manifest(path: Path) -> list[tuple[str, str]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, filename = line.split(None, 1)
        rows.append((filename.strip(), digest))
    return rows


def actual_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_files(root: Path, manifest: Path) -> list[tuple[str, str]]:
    entries = parse_manifest(manifest)
    for filename, expected in entries:
        actual = actual_sha256(root / filename)
        if actual != expected:
            raise SystemExit(f"checksum mismatch: {filename}: expected {expected}, got {actual}")
    return entries


def dsn_from_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing DSN environment variable: {name}")
    return value


def applied_by() -> str:
    return os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"


def ensure_supported_server(conn) -> str:
    """Refuse to touch anything but MySQL >= 8.0 (review P2-19).

    The phase9 files rely on MySQL-only behaviour: `SET @ddl = IF(...)` plus
    PREPARE/EXECUTE guards, `information_schema` spellings, DATETIME(6)
    defaults and JSON columns.  MariaDB parses most of it but resolves
    `ADD COLUMN IF NOT EXISTS` and DDL atomicity differently, so a MariaDB run
    would silently diverge instead of failing loudly.
    """
    version = conn.execute(text("SELECT VERSION()")).scalar()
    comment = conn.execute(text("SELECT @@version_comment")).scalar()
    raw = str(version or "")
    fingerprint = f"{raw} / {comment or ''}"
    if not raw:
        raise SystemExit("unable to read server version: SELECT VERSION() returned nothing")
    if "mariadb" in fingerprint.lower():
        raise SystemExit(
            "unsupported server: the phase9 migrations target MySQL 8.0 only, "
            f"but this server reports MariaDB ({fingerprint}). "
            "MariaDB is not supported -- do not attempt to apply phase9 here."
        )
    head = raw.split("-", 1)[0]
    parts = head.split(".")
    try:
        parsed = (int(parts[0]), int(parts[1]))
    except (IndexError, ValueError):
        raise SystemExit(f"unable to parse server version: {fingerprint}") from None
    if parsed < MIN_MYSQL_VERSION:
        raise SystemExit(
            "unsupported server: the phase9 migrations require MySQL "
            f"{MIN_MYSQL_VERSION[0]}.{MIN_MYSQL_VERSION[1]} or newer, got {fingerprint}"
        )
    print(f"server version ok: {raw}")
    return raw[:128]


def split_statements(sql: str) -> list[str]:
    """Split a migration file into statements on unquoted, uncommented `;`.

    A plain `sql.split(";")` is wrong here.  The phase9 files carry `--`
    prose that contains semicolons, and splitting mid-comment leaves the tail
    of the comment stripped of its `--` marker at the head of the next chunk,
    which MySQL then rejects as a syntax error.  The quoted DDL inside
    `SET @ddl = IF(...)` has the same hazard.

    So the scanner skips `-- `/`#` line comments, `/* */` block comments, and
    `'`/`"`/`` ` `` quoted runs (honouring both doubled-quote and backslash
    escapes) and only treats a bare `;` as a boundary.  Comments are dropped
    from the emitted text; chunks that end up empty are skipped, otherwise
    MySQL rejects them as empty queries.
    """
    statements: list[str] = []
    buffer: list[str] = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        # MySQL only treats `--` as a comment when followed by whitespace/EOL.
        if sql.startswith("--", index) and (
            index + 2 >= length or sql[index + 2] in " \t\r\n"
        ):
            newline = sql.find("\n", index)
            index = length if newline == -1 else newline
            buffer.append(" ")
            continue
        if char == "#":
            newline = sql.find("\n", index)
            index = length if newline == -1 else newline
            buffer.append(" ")
            continue
        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            index = length if end == -1 else end + 2
            buffer.append(" ")
            continue
        if char in ("'", '"', "`"):
            buffer.append(char)
            index += 1
            while index < length:
                inner = sql[index]
                if inner == "\\" and char != "`":
                    buffer.append(sql[index:index + 2])
                    index += 2
                    continue
                if inner == char:
                    if index + 1 < length and sql[index + 1] == char:
                        buffer.append(sql[index:index + 2])
                        index += 2
                        continue
                    buffer.append(inner)
                    index += 1
                    break
                buffer.append(inner)
                index += 1
            continue
        if char == ";":
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer = []
            index += 1
            continue
        buffer.append(char)
        index += 1
    tail = "".join(buffer).strip()
    if tail:
        statements.append(tail)
    return statements


def record_ledger(engine, *, name, digest, duration_ms, success, server_version) -> None:
    """Write one ledger row on a connection of its own.

    On failure this must NOT share the connection that just blew up: MySQL
    implicitly commits DDL, so the half-applied file is already durable and the
    ledger row has to survive independently of the aborted transaction.
    """
    with engine.begin() as conn:
        conn.execute(text(LEDGER_UPSERT), {
            "name": name,
            "digest": digest,
            "applied_by": applied_by(),
            "duration": duration_ms,
            "success": 1 if success else 0,
            "server_version": server_version,
        })


def load_ledger(engine) -> tuple[dict[str, str], dict[str, str]]:
    with engine.connect() as conn:
        rows = list(conn.execute(text(
            "SELECT migration_name, sha256, success FROM schema_migration_history "
            "WHERE migration_name LIKE 'phase9%'"
        )))
    succeeded = {row[0]: row[1] for row in rows if int(row[2]) == 1}
    failed = {row[0]: row[1] for row in rows if int(row[2]) != 1}
    return succeeded, failed


def apply_one(engine, path: Path, filename: str, digest: str, server_version: str) -> None:
    """Run a single migration file in its own connection and transaction.

    Review P2-28: sharing one `engine.begin()` across all seven files was
    unsafe.  MySQL commits DDL implicitly, so a failure in file 5 left files
    1-4 applied with no ledger row at all -- unlocatable state.  Each file now
    owns its connection (session variables such as `@schema_name` must not
    cross connections) and writes its own ledger row: success=0 the moment it
    breaks, success=1 only after the last statement of the file went through.
    """
    statements = split_statements(path.read_text(encoding="utf-8"))
    started = time.monotonic()
    executed = 0
    try:
        with engine.begin() as conn:
            for statement in statements:
                conn.execute(text(statement))
                executed += 1
    except Exception as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        print(
            f"FAILED {filename}: statement {executed + 1}/{len(statements)} raised; "
            f"{executed} statement(s) already executed and MySQL has implicitly "
            f"committed their DDL. sha256={digest} server_version={server_version} "
            f"error={exc.__class__.__name__}: {exc}"
        )
        try:
            record_ledger(
                engine,
                name=filename,
                digest=digest,
                duration_ms=elapsed,
                success=False,
                server_version=server_version,
            )
        except Exception as ledger_exc:  # pragma: no cover - ledger is best effort
            print(f"WARNING: could not record the failed ledger row for {filename}: {ledger_exc}")
        raise
    elapsed = int((time.monotonic() - started) * 1000)
    record_ledger(
        engine,
        name=filename,
        digest=digest,
        duration_ms=elapsed,
        success=True,
        server_version=server_version,
    )
    print(f"applied {filename} ({elapsed}ms, {executed} statements)")


def apply_migrations(engine, root: Path, entries, server_version: str) -> None:
    succeeded, failed = load_ledger(engine)
    for filename, digest in entries:
        if filename in failed:
            print(f"retrying {filename}: a previous run recorded success=0")
        recorded = succeeded.get(filename)
        if recorded is not None:
            if recorded != digest:
                raise SystemExit(
                    f"database checksum mismatch: {filename}: "
                    f"ledger has {recorded}, manifest has {digest}"
                )
            print(f"skipped {filename} (already applied)")
            continue
        apply_one(engine, root / filename, filename, digest, server_version)


def verify_structure(engine) -> list[str]:
    """Confirm the DDL really landed, not just that the ledger says so (P2-29)."""
    with engine.connect() as conn:
        tables = {
            str(row[0]) for row in conn.execute(text(
                "SELECT TABLE_NAME FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE()"
            ))
        }
        columns = {
            (str(row[0]), str(row[1])) for row in conn.execute(text(
                "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE()"
            ))
        }
        indexes = {
            (str(row[0]), str(row[1])) for row in conn.execute(text(
                "SELECT TABLE_NAME, INDEX_NAME FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA = DATABASE()"
            ))
        }
    problems = []
    for table in REQUIRED_TABLES:
        if table not in tables:
            problems.append(f"missing table: {table}")
    for table, column in REQUIRED_COLUMNS:
        if (table, column) not in columns:
            problems.append(f"missing column: {table}.{column}")
    for table, index in REQUIRED_INDEXES:
        if (table, index) not in indexes:
            problems.append(f"missing index: {table}.{index}")
    return problems


def verify(engine, entries) -> None:
    succeeded, _failed = load_ledger(engine)
    problems = [
        f"missing/stale ledger entry: {name}"
        for name, digest in entries
        if succeeded.get(name) != digest
    ]
    problems.extend(verify_structure(engine))
    if problems:
        raise SystemExit(
            "phase9 verification failed:\n  " + "\n  ".join(problems)
        )
    print(
        f"phase9 verification ok: {len(entries)} ledger entries, "
        f"{len(REQUIRED_TABLES)} tables, {len(REQUIRED_COLUMNS)} columns, "
        f"{len(REQUIRED_INDEXES)} indexes"
    )


def run(dsn_env: str, manifest_file: str, mode: str) -> None:
    root = Path(__file__).resolve().parents[1] / "sql" / "migrations"
    manifest = root / Path(manifest_file).name
    entries = check_files(root, manifest)
    if mode == "check":
        print(f"phase9 checksum ok: {len(entries)} migrations")
        return
    engine = create_engine(dsn_from_env(dsn_env), pool_pre_ping=True)
    try:
        # P2-19: gate on the server version before a single DDL statement runs.
        with engine.begin() as conn:
            server_version = ensure_supported_server(conn)
            conn.execute(text(LEDGER_DDL))
        if mode == "verify":
            verify(engine, entries)
            return
        apply_migrations(engine, root, entries, server_version)
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn-env", default="DB_URL")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    modes = [name for name, enabled in (("check", args.check), ("apply", args.apply), ("verify", args.verify)) if enabled]
    if len(modes) != 1:
        parser.error("exactly one of --check/--apply/--verify is required")
    run(args.dsn_env, args.manifest, modes[0])


if __name__ == "__main__":
    main()
