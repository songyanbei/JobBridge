"""Fail-closed Phase 11 migration runner.

Only manifest-declared files are executable.  SQL and Python progress is
persisted in MySQL so process death never turns a partial migration into an
apparently successful one.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.exc import DBAPIError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.storage_reference_service import normalize_storage_reference
from scripts.phase11_cli_safety import run_safely

BACKEND_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = BACKEND_ROOT / "sql" / "migrations" / "phase11_manifest.json"
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
BUILD_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
STAGES = {"pre_cutover", "post_cutover", "verify", "down"}
KINDS = {"sql", "python", "verify_sql"}
TOP_FIELDS = {"schema_version", "mysql", "minimum_build", "steps"}
STEP_FIELDS = {
    "key", "stage", "kind", "path", "sha256", "requires", "resumable",
    "verify_sql", "cursor_key",
}

LEDGER_DDL = """CREATE TABLE IF NOT EXISTS `phase11_migration_ledger` (
 `migration_key` VARCHAR(128) NOT NULL, `script_sha256` CHAR(64) NOT NULL,
 `stage` ENUM('pre_cutover','post_cutover','verify','down') NOT NULL,
 `kind` ENUM('sql','python','verify_sql') NOT NULL,
 `status` ENUM('running','succeeded','failed','verified') NOT NULL,
 `attempt` INT UNSIGNED NOT NULL DEFAULT 0, `last_statement_ordinal` INT UNSIGNED NOT NULL DEFAULT 0,
 `resume_cursor_json` JSON NULL, `started_at` DATETIME(6) NULL, `completed_at` DATETIME(6) NULL,
 `cutover_resume_id` BIGINT UNSIGNED NULL, `build_probe_digest` CHAR(64) NULL,
 `executed_by` VARCHAR(128) NOT NULL, `error_code` VARCHAR(64) NULL,
 `verification_digest` CHAR(64) NULL,
 `updated_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
 PRIMARY KEY (`migration_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"""


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _exact_fields(value: dict, expected: set[str], *, where: str, optional: set[str] = frozenset()) -> None:
    unknown = set(value) - expected
    missing = expected - optional - set(value)
    if unknown or missing:
        raise ValueError(f"{where} fields invalid: missing={sorted(missing)} unknown={sorted(unknown)}")


def load_manifest(path: Path = MANIFEST) -> dict[str, Any]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("manifest must be an object")
    _exact_fields(doc, TOP_FIELDS, where="manifest")
    if doc["schema_version"] != 1:
        raise ValueError("unsupported manifest schema_version")
    mysql = doc["mysql"]
    _exact_fields(mysql, {"vendor", "min_version"}, where="mysql")
    if mysql != {"vendor": "mysql", "min_version": "8.0"}:
        raise ValueError("manifest supports Oracle MySQL 8.0 only")
    anchor = doc["minimum_build"]
    _exact_fields(anchor, {"ready", "build_number", "build_sha", "capabilities"}, where="minimum_build")
    if not isinstance(anchor["ready"], bool) or not isinstance(anchor["build_number"], int) or anchor["build_number"] < 0:
        raise ValueError("invalid minimum build readiness/number")
    if not isinstance(anchor["build_sha"], str) or not BUILD_SHA_RE.fullmatch(anchor["build_sha"]):
        raise ValueError("invalid minimum build SHA")
    if not isinstance(anchor["capabilities"], list) or not anchor["capabilities"] or any(not isinstance(x, str) or not x for x in anchor["capabilities"]):
        raise ValueError("minimum build capabilities must be non-empty strings")
    steps = doc["steps"]
    if not isinstance(steps, list) or not steps:
        raise ValueError("steps must be a non-empty list")
    keys: set[str] = set()
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError(f"step {index} must be an object")
        _exact_fields(step, STEP_FIELDS, where=f"step[{index}]", optional={"verify_sql", "cursor_key"})
        key = step["key"]
        if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9_]+", key) or key in keys:
            raise ValueError(f"invalid or duplicate step key: {key!r}")
        keys.add(key)
        if step["stage"] not in STAGES or step["kind"] not in KINDS:
            raise ValueError(f"invalid stage/kind for {key}")
        if not isinstance(step["resumable"], bool) or not isinstance(step["requires"], list):
            raise ValueError(f"invalid recovery contract for {key}")
        if not isinstance(step["sha256"], str) or not SHA_RE.fullmatch(step["sha256"]):
            raise ValueError(f"invalid sha256 for {key}")
        rel = step["path"]
        if not isinstance(rel, str) or not rel or Path(rel).is_absolute() or ".." in Path(rel).parts or "\\" in rel:
            raise ValueError(f"unsafe path for {key}")
        if step["kind"] in {"sql", "verify_sql"} and not isinstance(step.get("verify_sql"), str):
            raise ValueError(f"SQL step {key} requires verify_sql")
        if step["kind"] == "python" and (not isinstance(step.get("cursor_key"), str) or not step["cursor_key"]):
            raise ValueError(f"Python step {key} requires cursor_key")
    for step in steps:
        if any(not isinstance(dep, str) or dep not in keys for dep in step["requires"]):
            raise ValueError(f"unknown dependency for {step['key']}")
    pending = {s["key"]: set(s["requires"]) for s in steps}
    while pending:
        ready = {key for key, deps in pending.items() if not deps}
        if not ready:
            raise ValueError("manifest dependency cycle")
        for key in ready:
            pending.pop(key)
        for deps in pending.values():
            deps.difference_update(ready)
    return doc


def resolve_steps(doc: dict[str, Any], root: Path = BACKEND_ROOT) -> list[dict[str, Any]]:
    root = root.resolve()
    result = []
    for raw in doc["steps"]:
        step = dict(raw)
        target = (root / step["path"]).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"path escapes backend root: {step['key']}") from exc
        if not target.is_file():
            raise ValueError(f"migration file missing: {step['path']}")
        if file_sha256(target) != step["sha256"]:
            raise ValueError(f"checksum mismatch: {step['key']}")
        step["file"] = target
        result.append(step)
    return result


def check_manifest(path: Path = MANIFEST) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    doc = load_manifest(path)
    return doc, resolve_steps(doc, path.resolve().parents[2])


def ensure_mysql8(conn) -> str:
    version = str(conn.execute(text("SELECT VERSION()" )).scalar() or "")
    comment = str(conn.execute(text("SELECT @@version_comment")).scalar() or "")
    if "mariadb" in (version + " " + comment).lower():
        raise RuntimeError("unsupported_database_vendor")
    match = re.match(r"^(\d+)\.(\d+)", version)
    if not match or (int(match.group(1)), int(match.group(2))) < (8, 0):
        raise RuntimeError("mysql_8_required")
    return version


def split_sql(source: str) -> list[str]:
    statements, buf, quote, line_comment, block_comment = [], [], None, False, False
    i = 0
    while i < len(source):
        ch, nxt = source[i], source[i + 1] if i + 1 < len(source) else ""
        if line_comment:
            if ch == "\n": line_comment = False; buf.append(ch)
        elif block_comment:
            if ch == "*" and nxt == "/": block_comment = False; i += 1
        elif quote:
            buf.append(ch)
            if ch == "\\" and i + 1 < len(source): buf.append(source[i + 1]); i += 1
            elif ch == quote:
                if nxt == quote: buf.append(nxt); i += 1
                else: quote = None
        elif ch == "-" and nxt == "-" and (i + 2 == len(source) or source[i + 2].isspace()):
            line_comment = True; i += 1
        elif ch == "/" and nxt == "*":
            block_comment = True; i += 1
        elif ch in "'\"`": quote = ch; buf.append(ch)
        elif ch == ";":
            value = "".join(buf).strip()
            if value: statements.append(value)
            buf = []
        else: buf.append(ch)
        i += 1
    value = "".join(buf).strip()
    if value: statements.append(value)
    if quote or block_comment:
        raise ValueError("unterminated SQL quote/comment")
    return statements


def _probe_builds(anchor: dict, specs: Iterable[str]) -> str:
    if not anchor["ready"]:
        raise RuntimeError("minimum_build_anchor_not_ready")
    probes = list(specs)
    if not probes:
        raise RuntimeError("post_cutover_requires_build_probes")
    normalized = []
    required = set(anchor["capabilities"])
    for spec in probes:
        if "=" not in spec:
            raise ValueError("build probe must be URL=expected_sha")
        url, expected_sha = spec.rsplit("=", 1)
        if not BUILD_SHA_RE.fullmatch(expected_sha):
            raise ValueError("invalid deployment manifest SHA")
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - operator supplied URL
            payload = json.load(response)
        if int(payload.get("build_number", -1)) < anchor["build_number"]:
            raise RuntimeError("build_number_too_old")
        # The anchor is a minimum capability/build contract.  The exact SHA is
        # deployment-manifest evidence and therefore compares only with the
        # operator-declared instance SHA, not with the anchor's historical SHA.
        if payload.get("build_sha") != expected_sha:
            raise RuntimeError("deployment_sha_mismatch")
        if not required <= set(payload.get("capabilities") or []):
            raise RuntimeError("build_capability_missing")
        normalized.append({"url": url, "sha": expected_sha, "number": payload["build_number"]})
    return hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()


def _bootstrap(engine) -> None:
    with engine.begin() as conn:
        ensure_mysql8(conn)
        conn.execute(text(LEDGER_DDL))


def _ledger_table_exists(engine) -> bool:
    with engine.connect() as conn:
        return bool(conn.execute(text("""SELECT COUNT(*)
          FROM information_schema.tables
          WHERE table_schema=DATABASE() AND table_name='phase11_migration_ledger'""")).scalar())


@contextmanager
def _migration_lock(engine):
    """Serialize runner processes without relying on a transactional row."""
    conn = engine.connect()
    acquired = False
    try:
        acquired = int(conn.execute(text(
            "SELECT GET_LOCK('phase11_resume_migration_runner',10)"
        )).scalar() or 0) == 1
        if not acquired:
            raise RuntimeError("phase11_migration_lock_unavailable")
        yield
    finally:
        if acquired:
            conn.execute(text(
                "SELECT RELEASE_LOCK('phase11_resume_migration_runner')"
            ))
        conn.close()


def _ledger(engine, key: str):
    with engine.connect() as conn:
        return conn.execute(text("SELECT * FROM phase11_migration_ledger WHERE migration_key=:key"), {"key": key}).mappings().first()


def _mark_running(engine, step: dict, *, executed_by: str, build_digest: str | None, cutover_resume_id: int | None) -> None:
    with engine.begin() as conn:
        conn.execute(text("""INSERT INTO phase11_migration_ledger
          (migration_key,script_sha256,stage,kind,status,attempt,last_statement_ordinal,started_at,cutover_resume_id,build_probe_digest,executed_by)
          VALUES (:key,:sha,:stage,:kind,'running',1,0,UTC_TIMESTAMP(6),:cutover,:probe,:actor)
          ON DUPLICATE KEY UPDATE status='running',attempt=attempt+1,
          completed_at=NULL,error_code=NULL,executed_by=:actor"""), {
            "key": step["key"], "sha": step["sha256"], "stage": step["stage"], "kind": step["kind"],
            "cutover": cutover_resume_id, "probe": build_digest, "actor": executed_by,
        })


def _mark_failed(engine, key: str, code: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("UPDATE phase11_migration_ledger SET status='failed',error_code=:code WHERE migration_key=:key"), {"key": key, "code": code[:64]})


def _mark_succeeded(engine, key: str, *, digest: str | None = None) -> None:
    with engine.begin() as conn:
        conn.execute(text("""UPDATE phase11_migration_ledger SET status=:status,completed_at=UTC_TIMESTAMP(6),
          error_code=NULL,verification_digest=COALESCE(:digest,verification_digest) WHERE migration_key=:key"""),
          {"key": key, "digest": digest, "status": "verified" if digest else "succeeded"})


def _verify_query(conn, query: str) -> Any:
    result = conn.execute(text(query))
    row = result.mappings().first()
    if row is None:
        return None
    values = list(row.values())
    if len(values) == 1 and isinstance(values[0], (int, bool)) and int(values[0]) != 0:
        raise RuntimeError("step_verify_failed")
    return dict(row)


def _duplicate_ddl(exc: DBAPIError) -> bool:
    code = getattr(getattr(exc, "orig", None), "args", [None])[0]
    return code in {1050, 1060, 1061}


def _already_applied_destructive_ddl(exc: DBAPIError, statement: str) -> bool:
    code = getattr(getattr(exc, "orig", None), "args", [None])[0]
    if code not in {1051, 1091}:
        return False
    return bool(re.match(
        r"\s*(DROP\s+TABLE|ALTER\s+TABLE\s+`?[a-z0-9_]+`?\s+DROP\s+(?:KEY|COLUMN))",
        statement, re.I,
    ))


def _validate_duplicate_ddl_shape(engine, statement: str) -> None:
    """Accept replayed additive DDL only when the existing object is exact.

    MySQL commits DDL independently.  A crash may therefore occur after the
    object was created but before its ordinal was checkpointed.  Error-code
    swallowing alone would also accept a pre-existing object with an unsafe
    shape, so replay performs a narrow information_schema proof first.
    """
    column_match = re.search(
        r"ALTER\s+TABLE\s+`?([a-z0-9_]+)`?\s+ADD\s+COLUMN\s+`?([a-z0-9_]+)`?",
        statement,
        re.IGNORECASE,
    )
    index_match = re.search(
        r"ALTER\s+TABLE\s+`?([a-z0-9_]+)`?\s+ADD\s+(?:KEY|INDEX)\s+`?([a-z0-9_]+)`?\s*\(([^)]+)\)",
        statement,
        re.IGNORECASE,
    )
    modify_match = re.search(
        r"ALTER\s+TABLE\s+`?([a-z0-9_]+)`?\s+MODIFY\s+COLUMN\s+`?([a-z0-9_]+)`?",
        statement, re.IGNORECASE,
    )
    with engine.connect() as conn:
        if column_match:
            table_name, column_name = column_match.groups()
            actual = conn.execute(text("""SELECT DATA_TYPE,DATETIME_PRECISION,IS_NULLABLE,
              CHARACTER_MAXIMUM_LENGTH FROM information_schema.columns
              WHERE table_schema=DATABASE() AND table_name=:table AND column_name=:column"""),
              {"table": table_name, "column": column_name}).mappings().one_or_none()
            expected = {
                ("resume", "activated_at"): ("datetime", 6, "YES", None),
                ("resume", "candidate_expires_at"): ("datetime", 6, "YES", None),
                ("resume", "delist_reason"): ("varchar", None, "YES", 32),
            }.get((table_name.lower(), column_name.lower()))
            if expected and actual and tuple(actual.values()) == expected:
                return
            raise RuntimeError(f"duplicate_ddl_shape_mismatch:{table_name}.{column_name}")
        if index_match:
            table_name, index_name, raw_columns = index_match.groups()
            expected_columns = [part.strip().strip("`").lower() for part in raw_columns.split(",")]
            rows = conn.execute(text("""SELECT COLUMN_NAME,NON_UNIQUE FROM information_schema.statistics
              WHERE table_schema=DATABASE() AND table_name=:table AND index_name=:index
              ORDER BY SEQ_IN_INDEX"""),
              {"table": table_name, "index": index_name}).all()
            if rows and [str(row[0]).lower() for row in rows] == expected_columns and all(int(row[1]) == 1 for row in rows):
                return
            raise RuntimeError(f"duplicate_ddl_shape_mismatch:{table_name}.{index_name}")
        if modify_match:
            table_name, column_name = modify_match.groups()
            actual = conn.execute(text("""SELECT DATA_TYPE,DATETIME_PRECISION,IS_NULLABLE,
              COLUMN_DEFAULT,EXTRA FROM information_schema.columns
              WHERE table_schema=DATABASE() AND table_name=:table AND column_name=:column"""),
              {"table": table_name, "column": column_name}).mappings().one_or_none()
            expected = {
                ("resume", "expires_at"): (
                    "datetime",
                    6,
                    "NO" if re.search(r"\bNOT\s+NULL\b", statement, re.I) else "YES",
                ),
                ("resume", "created_at"): ("datetime", 6, "NO"),
                ("resume", "updated_at"): ("datetime", 6, "NO"),
                ("resume", "deleted_at"): ("datetime", 6, "YES"),
            }.get((table_name.lower(), column_name.lower()))
            if expected and actual and tuple(list(actual.values())[:3]) == expected:
                return
            raise RuntimeError(f"duplicate_ddl_shape_mismatch:{table_name}.{column_name}")
    raise RuntimeError("duplicate_ddl_not_provably_equivalent")


def _normalize_show_create(value: str, table_name: str) -> str:
    value = re.sub(r"AUTO_INCREMENT=\d+\s*", "", value)
    value = re.sub(r"CONSTRAINT `[^`]+_chk_\d+`", "CONSTRAINT `__check__`", value)
    # Descriptive table comments are not a migration safety property and may
    # differ between canonical fresh-schema and additive upgrade artefacts.
    value = re.sub(r"\s+COMMENT='(?:''|[^'])*'\s*$", "", value)
    value = value.replace(f"`{table_name}`", "`__phase11_table__`")
    # MySQL preserves DDL index order while SQLAlchemy may emit the same
    # independent indexes in set iteration order.  Index ordering is not part
    # of table semantics; column order inside each index remains significant.
    indexes = re.findall(
        r",\s*((?:UNIQUE\s+)?KEY\s+`[^`]+`\s*\([^)]*\))",
        value,
        flags=re.IGNORECASE,
    )
    if indexes:
        value = re.sub(
            r",\s*(?:UNIQUE\s+)?KEY\s+`[^`]+`\s*\([^)]*\)",
            "",
            value,
            flags=re.IGNORECASE,
        )
        closing = value.rfind(")")
        value = value[:closing] + ", " + ", ".join(sorted(indexes, key=str.lower)) + " " + value[closing:]
    return re.sub(r"\s+", " ", value).strip()


def _validate_existing_create_table_shape(
    engine, statement: str, expected_like_shapes: dict[str, str] | None = None,
) -> bool:
    """Prove an IF-NOT-EXISTS table against an engine-canonical twin."""
    match = re.match(r"\s*CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+`?([a-z0-9_]+)`?", statement, re.I)
    if not match:
        return False
    table_name = match.group(1)
    proof_name = f"__phase11_shape_{hashlib.sha256(statement.encode()).hexdigest()[:16]}"
    with engine.begin() as conn:
        exists = int(conn.execute(text("""SELECT COUNT(*) FROM information_schema.tables
          WHERE table_schema=DATABASE() AND table_name=:name"""), {"name": table_name}).scalar_one())
        if not exists:
            return False
        like_match = re.search(r"\s+LIKE\s+`?([a-z0-9_]+)`?\s*$", statement, re.I)
        if like_match:
            recorded = (expected_like_shapes or {}).get(table_name)
            if recorded is not None:
                actual = conn.exec_driver_sql(f"SHOW CREATE TABLE `{table_name}`").one()[1]
                if _normalize_show_create(actual, table_name) != recorded:
                    raise RuntimeError(f"duplicate_ddl_shape_mismatch:{table_name}")
                return True
            source_exists = int(conn.execute(text("""SELECT COUNT(*) FROM information_schema.tables
              WHERE table_schema=DATABASE() AND table_name=:name"""),
              {"name": like_match.group(1)}).scalar_one())
            if not source_exists:
                raise RuntimeError(f"missing_recorded_create_table_shape:{table_name}")
        conn.exec_driver_sql(f"DROP TABLE IF EXISTS `{proof_name}`")
        proof_sql = re.sub(
            r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+`?[a-z0-9_]+`?",
            f"CREATE TABLE `{proof_name}`", statement, count=1, flags=re.I,
        )
        try:
            conn.exec_driver_sql(proof_sql)
            actual = conn.exec_driver_sql(f"SHOW CREATE TABLE `{table_name}`").one()[1]
            expected = conn.exec_driver_sql(f"SHOW CREATE TABLE `{proof_name}`").one()[1]
        finally:
            conn.exec_driver_sql(f"DROP TABLE IF EXISTS `{proof_name}`")
    if _normalize_show_create(actual, table_name) != _normalize_show_create(expected, proof_name):
        raise RuntimeError(f"duplicate_ddl_shape_mismatch:{table_name}")
    return True


def _created_table_name(statement: str) -> str | None:
    match = re.match(
        r"\s*CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+`?([a-z0-9_]+)`?",
        statement,
        re.I,
    )
    return match.group(1) if match else None


def _completed_lifecycle_drop(
    statements: list[str], create_index: int, start_ordinal: int, table_name: str,
) -> bool:
    """Return true only for a CREATE whose exact later DROP is checkpointed.

    This is deliberately statement-local: a missing persistent backup remains
    a reconciliation failure, while a guard table that has completed its
    create/use/drop lifecycle is required to be absent on resume.
    """
    # Include the immediately next statement: it may have committed before the
    # runner durably advanced its ordinal.  Requiring the table to be absent
    # below distinguishes that exact recovery window from an unexecuted DROP.
    for later in statements[create_index + 1:min(len(statements), start_ordinal + 1)]:
        if _created_table_name(later) == table_name:
            return False
        drop = re.match(
            r"\s*DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?`?([a-z0-9_]+)`?\s*$",
            later,
            re.I,
        )
        if drop and drop.group(1).lower() == table_name.lower():
            return True
    return False


def _require_table_absent(engine, table_name: str) -> None:
    with engine.connect() as conn:
        count = conn.execute(text("""SELECT COUNT(*) FROM information_schema.tables
          WHERE table_schema=DATABASE() AND table_name=:name"""),
          {"name": table_name}).scalar_one()
    if count:
        raise RuntimeError(f"resume_temporary_table_lifecycle_mismatch:{table_name}")


def _record_create_like_shape(engine, migration_key: str, statement: str) -> None:
    create = _created_table_name(statement)
    like = re.search(r"\s+LIKE\s+`?([a-z0-9_]+)`?\s*$", statement, re.I)
    if not create or not like:
        return
    with engine.begin() as conn:
        actual = conn.exec_driver_sql(f"SHOW CREATE TABLE `{create}`").one()[1]
        raw = conn.execute(text("""SELECT resume_cursor_json
          FROM phase11_migration_ledger WHERE migration_key=:key FOR UPDATE"""),
          {"key": migration_key}).scalar_one_or_none()
        cursor = raw if isinstance(raw, dict) else json.loads(raw or "{}")
        shapes = cursor.setdefault("created_table_shapes", {})
        shapes[create] = _normalize_show_create(actual, create)
        conn.execute(text("""UPDATE phase11_migration_ledger
          SET resume_cursor_json=:cursor WHERE migration_key=:key"""), {
              "cursor": json.dumps(cursor, ensure_ascii=True, sort_keys=True),
              "key": migration_key,
          })


def _recorded_create_like_shapes(engine, migration_key: str) -> dict[str, str]:
    with engine.connect() as conn:
        raw = conn.execute(text("""SELECT resume_cursor_json
          FROM phase11_migration_ledger WHERE migration_key=:key"""),
          {"key": migration_key}).scalar_one_or_none()
    cursor = raw if isinstance(raw, dict) else json.loads(raw or "{}")
    shapes = cursor.get("created_table_shapes", {})
    if not isinstance(shapes, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in shapes.items()
    ):
        raise RuntimeError("invalid_recorded_create_table_shapes")
    return shapes


_DOWN_AUDIT_SPECS = {
    "source_resume": ("resume", None),
    "backup_resume": ("phase11_resume_down_backup", None),
    "source_replacement": ("resume_replacement", None),
    "backup_replacement": ("phase11_resume_down_replacement_backup", None),
    "source_assignment": ("resume_replacement_rollout_assignment", None),
    "backup_assignment": ("phase11_resume_down_assignment_backup", None),
    "source_media_issue": ("resume_media_isolation_issue", None),
    "backup_media_issue": ("phase11_resume_down_media_issue_backup", None),
    # The runner's own down ledger row necessarily changes after capture.  It
    # is excluded from both sides; every column of every retained ledger row
    # is still part of the mirror proof.
    "source_ledger": ("phase11_migration_ledger", "migration_key<>'phase11_resume_lifecycle_down'"),
    "backup_ledger": ("phase11_resume_down_ledger_backup", "migration_key<>'phase11_resume_lifecycle_down'"),
}


def _quoted_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise RuntimeError("unsafe_catalog_identifier")
    return f"`{value}`"


def _strong_table_digest(conn, table_name: str, where_sql: str | None) -> tuple[int, str]:
    """Stream a deterministic SHA-256 chain over every column and row.

    Column order comes from ordinal_position and row order from the complete
    primary key.  MySQL produces the canonical JSON scalar representation,
    including explicit JSON nulls, before hashing each row.  Streaming avoids
    GROUP_CONCAT truncation and is collision-resistant unlike CRC/XOR.
    """
    columns = [row[0] for row in conn.execute(text("""SELECT column_name
      FROM information_schema.columns WHERE table_schema=DATABASE()
      AND table_name=:table ORDER BY ordinal_position"""), {"table": table_name})]
    primary = [row[0] for row in conn.execute(text("""SELECT column_name
      FROM information_schema.statistics WHERE table_schema=DATABASE()
      AND table_name=:table AND index_name='PRIMARY' ORDER BY seq_in_index"""),
      {"table": table_name})]
    if not columns or not primary:
        raise RuntimeError(f"down_digest_shape_invalid:{table_name}")
    json_args = ",".join(_quoted_identifier(column) for column in columns)
    order = ",".join(_quoted_identifier(column) for column in primary)
    predicate = f" WHERE {where_sql}" if where_sql else ""
    query = (
        f"SELECT SHA2(CAST(JSON_ARRAY({json_args}) AS CHAR CHARACTER SET utf8mb4),256) "
        f"FROM {_quoted_identifier(table_name)}{predicate} ORDER BY {order}"
    )
    digest = hashlib.sha256(b"").hexdigest()
    row_count = 0
    for (row_hash,) in conn.exec_driver_sql(query):
        digest = hashlib.sha256((digest + str(row_hash)).encode("ascii")).hexdigest()
        row_count += 1
    return row_count, digest


def _strong_resume_expected_digest(conn, transformation: str) -> tuple[int, str]:
    """Digest the exact Resume image expected after a guarded down DML.

    The retained down backup remains the immutable pre-down evidence.  This
    projection changes only ``expires_at`` according to statement 27 or 28;
    every other business and lifecycle column is read verbatim from that
    backup.  Comparing it with the live Resume digest therefore permits the
    intended downgrade conversion without hiding unrelated row drift.
    """
    columns = [row[0] for row in conn.execute(text("""SELECT column_name
      FROM information_schema.columns WHERE table_schema=DATABASE()
      AND table_name='resume' ORDER BY ordinal_position"""))]
    primary = [row[0] for row in conn.execute(text("""SELECT column_name
      FROM information_schema.statistics WHERE table_schema=DATABASE()
      AND table_name='resume' AND index_name='PRIMARY' ORDER BY seq_in_index"""))]
    if not columns or not primary or "expires_at" not in columns:
        raise RuntimeError("down_digest_shape_invalid:resume")
    if transformation == "original":
        expires_expression = "b.`expires_at`"
    elif transformation == "backup_ttl":
        expires_expression = "COALESCE(b.`expires_at`,l.`expires_at`)"
    elif transformation == "candidate_ttl":
        ttl = _validated_down_ttl_days(conn)
        expires_expression = (
            "COALESCE(b.`expires_at`,l.`expires_at`,"
            "IF(b.`audit_status` IN ('pending','rejected') "
            "AND b.`activated_at` IS NULL,COALESCE(b.`candidate_expires_at`,"
            f"b.`created_at` + INTERVAL {ttl} DAY),NULL))"
        )
    else:
        raise RuntimeError("down_resume_projection_invalid")
    json_args = ",".join(
        expires_expression if column == "expires_at" else f"b.{_quoted_identifier(column)}"
        for column in columns
    )
    order = ",".join(f"b.{_quoted_identifier(column)}" for column in primary)
    query = (
        "SELECT SHA2(CAST(JSON_ARRAY(" + json_args
        + ") AS CHAR CHARACTER SET utf8mb4),256) "
        "FROM `phase11_resume_down_backup` b "
        "LEFT JOIN `phase11_resume_lifecycle_backup` l ON l.`resume_id`=b.`id` "
        "ORDER BY " + order
    )
    digest = hashlib.sha256(b"").hexdigest()
    row_count = 0
    for (row_hash,) in conn.exec_driver_sql(query):
        digest = hashlib.sha256((digest + str(row_hash)).encode("ascii")).hexdigest()
        row_count += 1
    return row_count, digest


def _validated_down_ttl_days(conn) -> int:
    """Return the unique canonical downgrade TTL or fail closed."""
    rows = conn.execute(text("""SELECT config_value,value_type FROM system_config
      WHERE config_key='ttl.resume.days'""")).all()
    if len(rows) != 1:
        raise RuntimeError("down_ttl_config_invalid")
    raw, value_type = rows[0]
    if value_type != "int" or not isinstance(raw, str) or not re.fullmatch(r"[0-9]+", raw):
        raise RuntimeError("down_ttl_config_invalid")
    value = int(raw)
    if not 1 <= value <= 3650 or str(value) != raw:
        raise RuntimeError("down_ttl_config_invalid")
    return value


def _validate_down_ttl_config(engine) -> None:
    with engine.connect() as conn:
        _validated_down_ttl_days(conn)


def _validate_candidate_ttl_config(engine) -> None:
    """Read-only preflight for the post-cutover lifecycle writer."""
    with engine.connect() as conn:
        rows = conn.execute(text("""SELECT config_value,value_type FROM system_config
          WHERE config_key='ttl.resume.candidate.days'""")).all()
    if len(rows) != 1:
        raise RuntimeError("candidate_ttl_config_invalid")
    raw, value_type = rows[0]
    if (
        value_type != "int"
        or not isinstance(raw, str)
        or not re.fullmatch(r"[0-9]+", raw)
    ):
        raise RuntimeError("candidate_ttl_config_invalid")
    value = int(raw)
    if not 1 <= value <= 365 or str(value) != raw:
        raise RuntimeError("candidate_ttl_config_invalid")


_DOWN_FREEZE_LOCK = "jobbridge.phase11.resume.down.freeze"
_DOWN_FREEZE_TABLES = (
    "resume",
    "resume_replacement",
    "resume_replacement_rollout_assignment",
    "resume_media_isolation_issue",
    "phase11_migration_ledger",
    "system_config",
)


def _down_freeze_trigger_name(table_name: str, operation: str) -> str:
    return f"phase11_down_freeze_{table_name}_{operation.lower()}"


def _drop_down_write_freeze(conn) -> None:
    for table_name in _DOWN_FREEZE_TABLES:
        for operation in ("INSERT", "UPDATE", "DELETE"):
            conn.exec_driver_sql(
                f"DROP TRIGGER IF EXISTS `{_down_freeze_trigger_name(table_name, operation)}`"
            )


def _install_down_write_freeze(conn) -> None:
    """Fence source/config DML for the check-to-DDL lifetime.

    MySQL DDL commits implicitly, so a transaction or a prior ``FOR UPDATE``
    cannot bridge a proof and the next destructive statement.  Durable
    triggers instead reject writes from every connection except the runner's
    named-lock owner.  The down-ledger row is exempt because statement
    ordinals are checkpointed on short independent connections; all other
    ledger mutations remain fenced.  Re-installation is safe after a crashed
    process (the named lock itself is released when that process disconnects).
    """
    acquired = conn.exec_driver_sql(
        "SELECT GET_LOCK(%s,0)", (_DOWN_FREEZE_LOCK,),
    ).scalar_one()
    if int(acquired or 0) != 1:
        raise RuntimeError("down_write_freeze_busy")
    try:
        _drop_down_write_freeze(conn)
        existing = {
            str(row[0]) for row in conn.execute(text("""SELECT table_name
              FROM information_schema.tables WHERE table_schema=DATABASE()
              AND table_name IN :tables""").bindparams(
                bindparam("tables", expanding=True)
            ), {"tables": list(_DOWN_FREEZE_TABLES)})
        }
        # A hard-killed runner loses its session named lock before it can drop
        # the durable triggers.  In that state the triggers must be inert so a
        # crashed downgrade cannot leave production writes permanently
        # blocked.  A subsequent runner acquires the lock, removes the stale
        # triggers above, and installs a fresh active fence.
        lock_owner = f"IS_USED_LOCK('{_DOWN_FREEZE_LOCK}')"
        owner_guard = (
            f"({lock_owner} IS NOT NULL AND {lock_owner}<>CONNECTION_ID())"
        )
        for table_name in _DOWN_FREEZE_TABLES:
            if table_name not in existing:
                continue
            for operation in ("INSERT", "UPDATE", "DELETE"):
                row = "NEW" if operation == "INSERT" else "OLD"
                protected = "TRUE"
                if table_name == "phase11_migration_ledger":
                    if operation == "UPDATE":
                        protected = (
                            "(OLD.`migration_key`<>'phase11_resume_lifecycle_down' "
                            "OR NEW.`migration_key`<>'phase11_resume_lifecycle_down')"
                        )
                    else:
                        protected = (
                            f"{row}.`migration_key`<>'phase11_resume_lifecycle_down'"
                        )
                elif table_name == "system_config":
                    if operation == "UPDATE":
                        protected = (
                            "(OLD.`config_key`='ttl.resume.days' "
                            "OR NEW.`config_key`='ttl.resume.days')"
                        )
                    else:
                        protected = f"{row}.`config_key`='ttl.resume.days'"
                trigger_name = _down_freeze_trigger_name(table_name, operation)
                conn.exec_driver_sql(f"""CREATE TRIGGER `{trigger_name}` BEFORE {operation}
                  ON `{table_name}` FOR EACH ROW BEGIN
                  IF ({protected}) AND ({owner_guard}) THEN
                    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='phase11_down_write_frozen';
                  END IF;
                  END""")
    except BaseException:
        # The outer runner only marks the fence as installed after this
        # function returns.  Therefore installation must unwind its own
        # partial DDL and named lock on every failure, including interrupts.
        try:
            _drop_down_write_freeze(conn)
        finally:
            conn.exec_driver_sql("SELECT RELEASE_LOCK(%s)", (_DOWN_FREEZE_LOCK,))
        raise


def _release_down_write_freeze(conn) -> None:
    try:
        _drop_down_write_freeze(conn)
    finally:
        conn.exec_driver_sql("SELECT RELEASE_LOCK(%s)", (_DOWN_FREEZE_LOCK,))


def _is_down_destructive_ddl(statement: str) -> bool:
    return bool(re.match(
        r"\s*(?:DROP\s+TABLE|ALTER\s+TABLE.+\s+(?:DROP|MODIFY)\s+)",
        statement, re.I | re.S,
    ))


def _validate_down_pre_destructive_statement(
    engine, statements: list[str], ordinal: int,
) -> None:
    """Re-prove frozen evidence immediately before DML/destructive DDL."""
    _validate_down_ttl_config(engine)
    _validate_down_export_audit(engine, statements, ordinal - 1)


def _capture_down_export_audits(engine) -> None:
    """Capture all source/backup proofs in one transaction, idempotently."""
    with engine.begin() as conn:
        captured = {
            name: _strong_table_digest(conn, table_name, where_sql)
            for name, (table_name, where_sql) in _DOWN_AUDIT_SPECS.items()
        }
        for suffix in ("resume", "replacement", "assignment", "media_issue", "ledger"):
            if captured[f"source_{suffix}"] != captured[f"backup_{suffix}"]:
                raise RuntimeError(f"down_export_pair_mismatch:{suffix}")
        for name, (row_count, row_digest) in captured.items():
            conn.execute(text("""INSERT INTO phase11_resume_down_export_audit
              (artifact_name,row_count,row_digest) VALUES(:name,:count,:digest)
              ON DUPLICATE KEY UPDATE row_count=VALUES(row_count),
              row_digest=VALUES(row_digest),captured_at=UTC_TIMESTAMP(6)"""), {
                  "name": name, "count": row_count, "digest": row_digest,
              })


def _completed_down_audit_names(statements: list[str], ordinal: int) -> set[str]:
    names: set[str] = set()
    for statement in statements[:ordinal]:
        if re.fullmatch(r"\s*SELECT\s+'phase11_capture_down_export_audits'\s*", statement, re.I):
            names.update(_DOWN_AUDIT_SPECS)
    return names


def _validate_down_export_audit(
    engine, statements: list[str], completed_ordinal: int,
) -> None:
    """Reconcile retained down evidence before any resumable statement runs.

    The ledger ordinal determines the exact set of audit rows that may exist.
    Each row is checked against its still-present source or backup table, and
    every completed source/backup pair must agree.  This makes a final-ordinal
    replay useful even after the phase-11 source tables have been dropped and
    prevents later REPLACE statements from silently covering earlier drift.
    """
    expected = _completed_down_audit_names(statements, completed_ordinal)
    audit_create_completed = any(
        _created_table_name(statement) == "phase11_resume_down_export_audit"
        for statement in statements[:completed_ordinal]
    )
    with engine.connect() as conn:
        audit_exists = bool(conn.execute(text("""SELECT COUNT(*)
          FROM information_schema.tables WHERE table_schema=DATABASE()
          AND table_name='phase11_resume_down_export_audit'""")).scalar_one())
        if not audit_exists:
            if audit_create_completed or expected:
                raise RuntimeError("down_export_audit_drift:missing_table")
            return
        rows = conn.execute(text("""SELECT artifact_name,row_count,row_digest
          FROM phase11_resume_down_export_audit""")).mappings().all()
        actual = {row["artifact_name"]: row for row in rows}
        marker_is_next = (
            completed_ordinal < len(statements)
            and re.fullmatch(
                r"\s*SELECT\s+'phase11_capture_down_export_audits'\s*",
                statements[completed_ordinal], re.I,
            ) is not None
        )
        # Crash after the atomic audit transaction committed but before the
        # marker ordinal advanced.  A complete, strongly revalidated ten-row
        # set is proof of that one precise window; partial/unknown rows remain
        # fail-closed.
        if marker_is_next and set(actual) == set(_DOWN_AUDIT_SPECS):
            expected = set(_DOWN_AUDIT_SPECS)
        # A CREATE TABLE may have committed immediately before its ordinal was
        # checkpointed.  It is safe only while still empty.
        if not audit_create_completed and not expected:
            if actual:
                raise RuntimeError("down_export_audit_drift:unexpected_rows")
            return
        if set(actual) != expected:
            raise RuntimeError("down_export_audit_drift:name_set")
        for artifact_name in sorted(expected):
            table_name, where_sql = _DOWN_AUDIT_SPECS[artifact_name]
            source_schema_retired = (
                artifact_name == "source_resume"
                and any(
                    re.match(
                        r"\s*ALTER\s+TABLE\s+`?resume`?\s+DROP\s+COLUMN\s+`?(?:activated_at|candidate_expires_at)`?",
                        statement,
                        re.I,
                    )
                    for statement in statements[:completed_ordinal]
                )
            )
            table_exists = bool(conn.execute(text("""SELECT COUNT(*)
              FROM information_schema.tables WHERE table_schema=DATABASE()
              AND table_name=:table_name"""), {
                  "table_name": table_name,
              }).scalar_one())
            if not table_exists:
                if artifact_name.startswith("backup_"):
                    raise RuntimeError(f"down_export_audit_drift:missing_{artifact_name}")
                continue
            if source_schema_retired:
                continue
            row_count, row_digest = _strong_table_digest(conn, table_name, where_sql)
            recorded = actual[artifact_name]
            if artifact_name == "source_resume" and not source_schema_retired:
                # The two downgrade DMLs intentionally change expires_at.
                # Preserve and revalidate the original backup/audit evidence,
                # then require the live row image to equal one exact expected
                # transformation for the durable/commit-before-checkpoint
                # window.  No other business-field change is accepted.
                dml_27 = 27
                dml_28 = 28
                if completed_ordinal < dml_27 - 1:
                    allowed = {"original"}
                elif completed_ordinal == dml_27 - 1:
                    allowed = {"original", "backup_ttl"}
                elif completed_ordinal == dml_27:
                    allowed = {"backup_ttl", "candidate_ttl"}
                else:
                    allowed = {"candidate_ttl"}
                expected_digests = {
                    _strong_resume_expected_digest(conn, transformation)
                    for transformation in allowed
                }
                if (row_count, row_digest) not in expected_digests:
                    raise RuntimeError("down_export_audit_drift:source_resume")
                continue
            if (
                int(recorded["row_count"]) != row_count
                or recorded["row_digest"] != row_digest
            ):
                raise RuntimeError(f"down_export_audit_drift:{artifact_name}")
        for suffix in ("resume", "replacement", "assignment", "media_issue", "ledger"):
            source = actual.get(f"source_{suffix}")
            backup = actual.get(f"backup_{suffix}")
            if source is None or backup is None:
                continue
            if (
                int(source["row_count"]) != int(backup["row_count"])
                or source["row_digest"] != backup["row_digest"]
            ):
                raise RuntimeError(f"down_export_audit_drift:{suffix}_pair")


def _run_sql(engine, step: dict, start_ordinal: int) -> None:
    statements = split_sql(step["file"].read_text(encoding="utf-8"))
    if step["stage"] == "down":
        # Re-run this immediately before every fresh/resumed down execution,
        # before backup refreshes, DML or destructive DDL can occur.
        _validate_down_ttl_config(engine)
        _validate_down_destructive_state(engine, statements, start_ordinal)
    recorded_like_shapes = _recorded_create_like_shapes(engine, step["key"])
    # A ledger ordinal is evidence, not proof. Reconcile every previously
    # checkpointed schema mutation before executing the next statement.
    for prior_index, prior in enumerate(statements[:start_ordinal]):
        if re.match(r"\s*CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS", prior, re.I):
            table_name = _created_table_name(prior)
            if table_name and _completed_lifecycle_drop(
                statements, prior_index, start_ordinal, table_name,
            ):
                _require_table_absent(engine, table_name)
            elif not _validate_existing_create_table_shape(
                engine, prior, recorded_like_shapes,
            ):
                raise RuntimeError("resume_schema_reconciliation_failed")
        elif re.match(r"\s*ALTER\s+TABLE.+\s+(?:ADD|MODIFY)\s+", prior, re.I | re.S):
            _validate_duplicate_ddl_shape(engine, prior)
        else:
            drop_table = re.match(r"\s*DROP\s+TABLE\s+`?([a-z0-9_]+)`?", prior, re.I)
            drop_member = re.match(
                r"\s*ALTER\s+TABLE\s+`?([a-z0-9_]+)`?\s+DROP\s+(KEY|COLUMN)\s+`?([a-z0-9_]+)`?",
                prior, re.I,
            )
            with engine.connect() as conn:
                if drop_table:
                    count = conn.execute(text("""SELECT COUNT(*) FROM information_schema.tables
                      WHERE table_schema=DATABASE() AND table_name=:name"""),
                      {"name": drop_table.group(1)}).scalar_one()
                    if count:
                        raise RuntimeError("resume_destructive_schema_reconciliation_failed")
                elif drop_member:
                    table_name, kind, member = drop_member.groups()
                    catalog = "statistics" if kind.upper() == "KEY" else "columns"
                    field = "index_name" if kind.upper() == "KEY" else "column_name"
                    count = conn.execute(text(f"""SELECT COUNT(*) FROM information_schema.{catalog}
                      WHERE table_schema=DATABASE() AND table_name=:table AND {field}=:member"""),
                      {"table": table_name, "member": member}).scalar_one()
                    if count:
                        raise RuntimeError("resume_destructive_schema_reconciliation_failed")
    if step["stage"] == "down":
        _validate_down_export_audit(engine, statements, start_ordinal)
    # Keep one physical connection for the whole SQL artefact.  For down this
    # connection owns the durable trigger fence, including across MySQL's
    # implicit DDL commits.
    execution_conn = engine.connect()
    explicit_transaction = False
    down_write_frozen = False
    try:
        if step["stage"] == "down":
            _install_down_write_freeze(execution_conn)
            down_write_frozen = True
        for ordinal, statement in enumerate(statements, 1):
            if ordinal <= start_ordinal:
                continue
            if step["stage"] == "down" and (
                ordinal in (27, 28)
                or (ordinal >= 26 and _is_down_destructive_ddl(statement))
            ):
                _validate_down_pre_destructive_statement(
                    engine, statements, ordinal,
                )
            if _validate_existing_create_table_shape(
                engine, statement, recorded_like_shapes,
            ):
                # Covers a crash after CREATE LIKE committed but before its
                # ordinal/cursor checkpoint: prove against the still-present
                # source, then persist the exact target shape before advancing.
                _record_create_like_shape(engine, step["key"], statement)
                with engine.begin() as conn:
                    conn.execute(text("UPDATE phase11_migration_ledger SET last_statement_ordinal=:ordinal WHERE migration_key=:key"), {"ordinal": ordinal, "key": step["key"]})
                continue
            try:
                if re.fullmatch(r"\s*START\s+TRANSACTION\s*", statement, re.I):
                    execution_conn.exec_driver_sql(statement)
                    explicit_transaction = True
                    continue
                if re.fullmatch(r"\s*COMMIT\s*", statement, re.I):
                    execution_conn.commit()
                    explicit_transaction = False
                elif re.fullmatch(r"\s*SELECT\s+'phase11_capture_down_export_audits'\s*", statement, re.I):
                    _capture_down_export_audits(engine)
                elif (
                    step["key"] == "phase11_resume_config_seed"
                    and re.match(r"\s*SIGNAL\s+SQLSTATE\s+'45000'", statement, re.I)
                ):
                    blockers = int(execution_conn.exec_driver_sql(
                        "SELECT COALESCE(@phase11_config_blockers,1)"
                    ).scalar_one())
                    if blockers:
                        execution_conn.exec_driver_sql(statement)
                else:
                    execution_conn.exec_driver_sql(statement)
                    if not explicit_transaction:
                        execution_conn.commit()
            except DBAPIError as exc:
                execution_conn.rollback()
                explicit_transaction = False
                if _already_applied_destructive_ddl(exc, statement) and step["stage"] == "down":
                    pass
                elif not _duplicate_ddl(exc):
                    raise
                else:
                    _validate_duplicate_ddl_shape(engine, statement)
            if explicit_transaction:
                # The entire explicit SQL transaction is one resumable unit.
                # Its internal ordinals cannot be evidence until COMMIT.
                continue
            _after_sql_commit_before_checkpoint(step, ordinal, statement)
            _record_create_like_shape(engine, step["key"], statement)
            with engine.begin() as conn:
                conn.execute(text("UPDATE phase11_migration_ledger SET last_statement_ordinal=:ordinal WHERE migration_key=:key"), {"ordinal": ordinal, "key": step["key"]})
            _after_sql_checkpoint(step, ordinal, statement)
    finally:
        if explicit_transaction:
            execution_conn.rollback()
        if down_write_frozen:
            _release_down_write_freeze(execution_conn)
        execution_conn.close()
    if step["stage"] == "down":
        _validate_down_export_audit(engine, statements, len(statements))
    with engine.connect() as conn:
        _verify_query(conn, step["verify_sql"])


def _after_sql_commit_before_checkpoint(step: dict, ordinal: int, statement: str) -> None:
    """Deterministic crash-injection hook for the DML/DDL checkpoint window."""


def _after_sql_checkpoint(step: dict, ordinal: int, statement: str) -> None:
    """Deterministic crash-injection hook after the durable ordinal update."""


def _normalize_resume_cursor(value: Any, *, source: str) -> dict[str, Any]:
    """Return the canonical JSON-object form used at the ledger boundary.

    MySQL JSON values may be returned by the driver either as decoded Python
    objects or as their JSON text.  Normalizing here prevents a resumed cursor
    string from being JSON-encoded a second time before it reaches a Python
    reconciliation tool.
    """
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{source}_invalid_json") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{source}_must_be_object")
    return value


def _verify_media_registry_projection(conn) -> None:
    """Prove the hash-only registry is an exact projection of Resume images."""
    expected: dict[tuple[int, str, str], int] = {}
    rows = conn.execute(text("SELECT id,images FROM resume ORDER BY id")).mappings()
    for row in rows:
        raw_images = row["images"]
        invalid_json = False
        if isinstance(raw_images, str):
            try:
                raw_images = json.loads(raw_images)
            except json.JSONDecodeError:
                raw_images = None
                invalid_json = True
        if invalid_json:
            key_hash = hashlib.sha256(f"invalid-json:{row['id']}".encode()).hexdigest()
            expected[(int(row["id"]), key_hash, "invalid")] = 1
        values = [] if raw_images is None else raw_images
        if not isinstance(values, list):
            values = [None]
        for raw in values:
            try:
                key = normalize_storage_reference(raw)
                key_hash = hashlib.sha256(key.encode()).hexdigest()
                kind = "valid"
            except (TypeError, ValueError):
                key_hash = hashlib.sha256(
                    ("invalid-reference:" + json.dumps(
                        raw, ensure_ascii=True, sort_keys=True, default=str,
                    )).encode()
                ).hexdigest()
                kind = "invalid"
            item = (int(row["id"]), key_hash, kind)
            expected[item] = expected.get(item, 0) + 1
    actual = {
        (int(row["resume_id"]), str(row["key_hash"]), str(row["reference_kind"])):
            int(row["reference_count"])
        for row in conn.execute(text("""SELECT resume_id,key_hash,reference_kind,reference_count
          FROM phase11_resume_media_key_scan""")).mappings()
    }
    if actual != expected:
        raise RuntimeError("media_registry_projection_drift")


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _run_python(engine, dsn: str, step: dict, cursor: Any, *,
                redis_dsn: str | None, redis_namespace: str | None) -> str:
    normalized_cursor = _normalize_resume_cursor(cursor, source="ledger_resume_cursor")
    # Refuse to start a child process when the ledger no longer matches the
    # cursor handed to the runner.  This makes audit tampering fail closed
    # before a migration script can perform any business writes.  A pristine
    # cursor is the only state allowed to have no verification digest yet.
    # Old, never-checkpointed cursors may legitimately predate the embedded
    # audit envelope; the child canonicalizes those once.  Every cursor emitted
    # by a Phase 11 child contains ``audit_summary`` and is verified before a
    # resumed child is allowed to run.
    if "audit_summary" in normalized_cursor:
        with engine.begin() as conn:
            row = conn.execute(text("""SELECT resume_cursor_json,verification_digest
              FROM phase11_migration_ledger WHERE migration_key=:key FOR UPDATE"""),
              {"key": step["key"]}).mappings().one()
            durable_cursor = _normalize_resume_cursor(
                row["resume_cursor_json"], source="durable_python_step_resume_cursor",
            )
            if durable_cursor != normalized_cursor:
                raise RuntimeError("python_step_checkpoint_cursor_mismatch")
            durable_summary = durable_cursor.get("audit_summary")
            durable_digest = row["verification_digest"]
            if (not isinstance(durable_summary, dict)
                    or durable_digest != _canonical_digest(durable_summary)):
                raise RuntimeError("python_step_checkpoint_digest_mismatch")
    command = [
        sys.executable, str(step["file"]), "--dsn", dsn, "--apply",
        "--resume-cursor-json", json.dumps(normalized_cursor, separators=(",", ":"), sort_keys=True),
    ]
    if step["key"] == "phase11_resume_orphan_target_reconcile":
        if not redis_dsn or not redis_namespace:
            raise RuntimeError("orphan_reconcile_requires_explicit_redis_target")
        command.extend(["--redis-dsn", redis_dsn, "--redis-namespace", redis_namespace])
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode:
        raise RuntimeError("python_step_failed")
    try:
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError("python_step_invalid_result") from exc
    if payload.get("status") != "succeeded":
        raise RuntimeError("python_step_incomplete")
    cursor_value = _normalize_resume_cursor(
        payload.get(step["cursor_key"]), source="python_step_resume_cursor"
    )
    audit_summary = payload.get("audit_summary")
    if not isinstance(audit_summary, dict) or not audit_summary:
        raise RuntimeError("python_step_audit_summary_missing")
    # Every child batch commits its cursor and canonical cumulative audit
    # digest in the same InnoDB transaction.  The runner must only attest what
    # is already durable; writing either value here would recreate a
    # commit-before-audit crash window.
    audit_digest = _canonical_digest(audit_summary)
    with engine.begin() as conn:
        row = conn.execute(text("""SELECT resume_cursor_json,verification_digest
          FROM phase11_migration_ledger WHERE migration_key=:key FOR UPDATE"""),
          {"key": step["key"]}).mappings().one()
        durable_cursor = _normalize_resume_cursor(
            row["resume_cursor_json"], source="durable_python_step_resume_cursor",
        )
        if durable_cursor != cursor_value:
            raise RuntimeError("python_step_checkpoint_cursor_mismatch")
        if durable_cursor.get("audit_summary") != audit_summary:
            raise RuntimeError("python_step_checkpoint_audit_mismatch")
        if row["verification_digest"] != audit_digest:
            raise RuntimeError("python_step_checkpoint_digest_mismatch")
    return audit_digest


def _validate_ledger(engine, steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Reject metadata that cannot be explained by the pinned manifest."""
    declared = {step["key"]: step for step in steps}
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT * FROM phase11_migration_ledger")).mappings().all()
    found = {row["migration_key"]: row for row in rows}
    unknown = set(found) - set(declared)
    if unknown:
        raise RuntimeError("unknown_ledger_entry:" + ",".join(sorted(unknown)))
    for key, row in found.items():
        step = declared[key]
        if row["script_sha256"] != step["sha256"]:
            raise RuntimeError(f"ledger_checksum_drift:{key}")
        if row["stage"] != step["stage"] or row["kind"] != step["kind"]:
            raise RuntimeError(f"ledger_contract_drift:{key}")
        if row["status"] not in {"running", "succeeded", "failed", "verified"}:
            raise RuntimeError(f"ledger_unknown_status:{key}")
        if row["status"] in {"succeeded", "verified"}:
            for dependency in step["requires"]:
                dependency_row = found.get(dependency)
                if not dependency_row or dependency_row["status"] not in {"succeeded", "verified"}:
                    raise RuntimeError(f"ledger_dependency_drift:{key}")
        if step["kind"] == "python" and row["status"] == "succeeded":
            digest = row["verification_digest"]
            if not isinstance(digest, str) or not SHA_RE.fullmatch(digest):
                raise RuntimeError(f"ledger_python_audit_missing:{key}")
    return found


def _down_destructive_progress_exists(engine, step: dict, ordinal: int) -> bool:
    """Detect only the exact commit-before-checkpoint down recovery window.

    A fresh down always receives the full additive-shape preflight.  Resume may
    bypass that preflight only when one of the destructive statements already
    evidenced by the ledger, or the immediately next statement, has its exact
    post-commit shape.  The SQL reconciler subsequently proves every ordinal.
    """
    statements = split_sql(step["file"].read_text(encoding="utf-8"))
    if ordinal < 0 or ordinal > len(statements):
        raise RuntimeError("down_ledger_ordinal_out_of_range")
    candidates = statements[:min(len(statements), ordinal + 1)]
    with engine.connect() as conn:
        for statement in candidates:
            drop_table = re.match(r"\s*DROP\s+TABLE\s+`?([a-z0-9_]+)`?", statement, re.I)
            drop_member = re.match(
                r"\s*ALTER\s+TABLE\s+`?([a-z0-9_]+)`?\s+DROP\s+(KEY|COLUMN)\s+`?([a-z0-9_]+)`?",
                statement, re.I,
            )
            modify = re.match(
                r"\s*ALTER\s+TABLE\s+`?resume`?\s+MODIFY\s+COLUMN\s+`?expires_at`?.*NOT\s+NULL",
                statement, re.I | re.S,
            )
            if drop_table:
                if drop_table.group(1).lower() not in {
                    "resume_replacement", "resume_replacement_rollout_assignment",
                    "resume_media_isolation_issue",
                }:
                    continue
                exists = int(conn.execute(text("""SELECT COUNT(*) FROM information_schema.tables
                  WHERE table_schema=DATABASE() AND table_name=:name"""),
                  {"name": drop_table.group(1)}).scalar_one())
                if not exists:
                    return True
            elif drop_member:
                table_name, kind, member = drop_member.groups()
                catalog = "statistics" if kind.upper() == "KEY" else "columns"
                field = "index_name" if kind.upper() == "KEY" else "column_name"
                exists = int(conn.execute(text(f"""SELECT COUNT(*) FROM information_schema.{catalog}
                  WHERE table_schema=DATABASE() AND table_name=:table AND {field}=:member"""),
                  {"table": table_name, "member": member}).scalar_one())
                if not exists:
                    return True
            elif modify:
                nullable = conn.execute(text("""SELECT IS_NULLABLE FROM information_schema.columns
                  WHERE table_schema=DATABASE() AND table_name='resume'
                    AND column_name='expires_at'""")).scalar_one_or_none()
                if nullable == "NO":
                    return True
    return False


def _validate_down_destructive_state(
    engine, statements: list[str], completed_ordinal: int,
) -> None:
    """Prove all persistent destructive targets around the ledger boundary.

    Statements before the durable ordinal must have their post-shape, future
    statements must retain their pre-shape, and only the immediately next
    statement may have either shape (the commit-before-checkpoint window).
    Temporary guard tables are independently reconciled by their CREATE/DROP
    lifecycle and are not persistent schema targets.
    """
    persistent_tables = {
        "resume_replacement", "resume_replacement_rollout_assignment",
        "resume_media_isolation_issue",
    }
    allowed_prefixed = {
        "phase11_migration_ledger", "phase11_resume_media_key_scan",
        "phase11_resume_lifecycle_backup", "phase11_down_guard",
        "phase11_export_guard", "phase11_null_ttl_guard",
        "phase11_resume_down_backup", "phase11_resume_down_replacement_backup",
        "phase11_resume_down_assignment_backup", "phase11_resume_down_media_issue_backup",
        "phase11_resume_down_ledger_backup", "phase11_resume_down_export_audit",
    }
    with engine.connect() as conn:
        prefixed = {str(row[0]) for row in conn.execute(text("""SELECT table_name
          FROM information_schema.tables WHERE table_schema=DATABASE()
            AND LEFT(table_name,8)='phase11_'"""))}
        if prefixed - allowed_prefixed:
            raise RuntimeError("down_unknown_schema_dependency")
        for ordinal, statement in enumerate(statements, 1):
            expected_post = ordinal <= completed_ordinal
            next_window = ordinal == completed_ordinal + 1
            drop_table = re.match(r"\s*DROP\s+TABLE\s+`?([a-z0-9_]+)`?", statement, re.I)
            drop_member = re.match(
                r"\s*ALTER\s+TABLE\s+`?resume`?\s+DROP\s+(KEY|COLUMN)\s+`?([a-z0-9_]+)`?",
                statement, re.I,
            )
            modify = re.match(
                r"\s*ALTER\s+TABLE\s+`?resume`?\s+MODIFY\s+COLUMN\s+`?expires_at`?.*NOT\s+NULL",
                statement, re.I | re.S,
            )
            if drop_table and drop_table.group(1).lower() in persistent_tables:
                exists = bool(conn.execute(text("""SELECT COUNT(*) FROM information_schema.tables
                  WHERE table_schema=DATABASE() AND table_name=:name"""), {
                    "name": drop_table.group(1),
                }).scalar_one())
                if not next_window and exists == expected_post:
                    raise RuntimeError("resume_destructive_schema_reconciliation_failed")
            elif drop_member:
                kind, member = drop_member.groups()
                catalog = "statistics" if kind.upper() == "KEY" else "columns"
                field = "index_name" if kind.upper() == "KEY" else "column_name"
                exists = bool(conn.execute(text(f"""SELECT COUNT(*)
                  FROM information_schema.{catalog} WHERE table_schema=DATABASE()
                    AND table_name='resume' AND {field}=:member"""), {
                    "member": member,
                }).scalar_one())
                if not next_window and exists == expected_post:
                    raise RuntimeError("resume_destructive_schema_reconciliation_failed")
            elif modify:
                nullable = conn.execute(text("""SELECT IS_NULLABLE
                  FROM information_schema.columns WHERE table_schema=DATABASE()
                    AND table_name='resume' AND column_name='expires_at'""")).scalar_one_or_none()
                is_post = nullable == "NO"
                if nullable not in {"YES", "NO"} or (not next_window and is_post != expected_post):
                    raise RuntimeError("resume_destructive_schema_reconciliation_failed")

        external_fks = int(conn.execute(text("""SELECT COUNT(*)
          FROM information_schema.key_column_usage
          WHERE constraint_schema=DATABASE() AND referenced_table_name IS NOT NULL
            AND (table_name IN ('resume_replacement','resume_replacement_rollout_assignment',
                 'resume_media_isolation_issue')
              OR referenced_table_name IN ('resume_replacement','resume_replacement_rollout_assignment',
                 'resume_media_isolation_issue')
              OR (table_name='resume' AND column_name IN
                 ('activated_at','candidate_expires_at','delist_reason')))""")).scalar_one())
        if external_fks:
            raise RuntimeError("down_unknown_schema_dependency")
        trigger_rows = conn.execute(text("""SELECT EVENT_OBJECT_TABLE,ACTION_STATEMENT
          FROM information_schema.triggers WHERE trigger_schema=DATABASE()
            AND event_object_table IN ('resume','resume_replacement',
              'resume_replacement_rollout_assignment','resume_media_isolation_issue')""")).all()
        check_rows = conn.execute(text("""SELECT tc.TABLE_NAME,cc.CHECK_CLAUSE
          FROM information_schema.table_constraints tc
          JOIN information_schema.check_constraints cc
            ON cc.CONSTRAINT_SCHEMA=tc.CONSTRAINT_SCHEMA
           AND cc.CONSTRAINT_NAME=tc.CONSTRAINT_NAME
          WHERE tc.CONSTRAINT_SCHEMA=DATABASE() AND tc.CONSTRAINT_TYPE='CHECK'
            AND tc.TABLE_NAME IN ('resume','resume_replacement',
              'resume_replacement_rollout_assignment','resume_media_isolation_issue')""")).all()
        phase_columns = ("activated_at", "candidate_expires_at", "delist_reason")
        unknown_triggers = [
            row for row in trigger_rows
            if str(row[0]) != "resume"
            or any(column in str(row[1]).lower() for column in phase_columns)
        ]
        unknown_checks = [
            row for row in check_rows
            if str(row[0]) != "resume"
            or any(column in str(row[1]).lower() for column in phase_columns)
        ]
        if unknown_triggers or unknown_checks:
            raise RuntimeError("down_unknown_schema_dependency")


def _down_preflight(engine) -> None:
    """Prove the complete additive shape before the first destructive step.

    Row-level blockers are necessary but insufficient: an operator or a
    partially applied hotfix may have added, removed or changed a constraint.
    The down script must never guess how to remove an unknown schema.
    """
    additive_sql = BACKEND_ROOT / "sql" / "migrations" / "phase11_001_resume_lifecycle_additive.sql"
    additive_statements = split_sql(additive_sql.read_text(encoding="utf-8"))
    expected_tables = {
        _created_table_name(statement): statement
        for statement in additive_statements
        if _created_table_name(statement)
    }
    expected_names = set(expected_tables)
    if expected_names != {
        "phase11_migration_ledger", "resume_replacement",
        "resume_replacement_rollout_assignment", "resume_media_isolation_issue",
        "phase11_resume_media_key_scan", "phase11_resume_lifecycle_backup",
    }:
        raise RuntimeError("down_shape_contract_invalid")
    for table_name, statement in expected_tables.items():
        if not _validate_existing_create_table_shape(engine, statement):
            raise RuntimeError(f"down_unknown_schema_dependency:missing_table:{table_name}")

    expected_resume_columns = {
        "activated_at": ("datetime", 6, "YES", None, "", None),
        "candidate_expires_at": ("datetime", 6, "YES", None, "", None),
        "expires_at": ("datetime", 6, "YES", None, "", None),
        "delist_reason": ("varchar", None, "YES", None, "", 32),
        "created_at": ("datetime", 6, "NO", "current_timestamp(6)", "DEFAULT_GENERATED", None),
        "updated_at": (
            "datetime", 6, "NO", "current_timestamp(6)",
            "DEFAULT_GENERATED on update CURRENT_TIMESTAMP(6)", None,
        ),
        "deleted_at": ("datetime", 6, "YES", None, "", None),
    }
    phase_tables = set(expected_tables)
    down_artifacts = {
        "phase11_down_guard", "phase11_export_guard", "phase11_null_ttl_guard",
        "phase11_resume_down_backup", "phase11_resume_down_replacement_backup",
        "phase11_resume_down_assignment_backup", "phase11_resume_down_media_issue_backup",
        "phase11_resume_down_ledger_backup", "phase11_resume_down_export_audit",
    }
    phase_columns = {"activated_at", "candidate_expires_at", "delist_reason"}
    query = """SELECT
      (SELECT COUNT(*) FROM resume_replacement WHERE lifecycle_status IN ('awaiting_review','conflict')) AS active_relations,
      (SELECT COUNT(*) FROM target_cleanup_task WHERE target_type='resume' AND status <> 'succeeded') AS cleanup_pending,
      (SELECT COUNT(*) FROM resume_media_isolation_issue WHERE status <> 'resolved') AS media_pending,
      (SELECT COUNT(*) FROM resume r LEFT JOIN phase11_resume_lifecycle_backup b ON b.resume_id=r.id
       WHERE r.expires_at IS NULL AND NOT (r.audit_status IN ('pending','rejected') AND r.activated_at IS NULL) AND b.resume_id IS NULL) AS unknown_rows"""
    with engine.connect() as conn:
        values = conn.execute(text(query)).mappings().one()
        actual_columns = conn.execute(text("""SELECT COLUMN_NAME,DATA_TYPE,DATETIME_PRECISION,
          IS_NULLABLE,COLUMN_DEFAULT,EXTRA,CHARACTER_MAXIMUM_LENGTH
          FROM information_schema.columns WHERE table_schema=DATABASE()
            AND table_name='resume' AND column_name IN
            ('activated_at','candidate_expires_at','expires_at','delist_reason',
             'created_at','updated_at','deleted_at')""")).mappings().all()
        actual_indexes = conn.execute(text("""SELECT INDEX_NAME,NON_UNIQUE,SEQ_IN_INDEX,COLUMN_NAME
          FROM information_schema.statistics WHERE table_schema=DATABASE() AND table_name='resume'
          ORDER BY INDEX_NAME,SEQ_IN_INDEX""")).mappings().all()
        prefixed_tables = {str(row[0]) for row in conn.execute(text("""SELECT table_name
          FROM information_schema.tables WHERE table_schema=DATABASE()
            AND LEFT(table_name,8)='phase11_'"""))}
        external_fks = int(conn.execute(text("""SELECT COUNT(*)
          FROM information_schema.key_column_usage
          WHERE constraint_schema=DATABASE() AND referenced_table_name IS NOT NULL
            AND (table_name IN ('resume_replacement','resume_replacement_rollout_assignment',
                 'resume_media_isolation_issue','phase11_migration_ledger',
                 'phase11_resume_media_key_scan','phase11_resume_lifecycle_backup')
              OR referenced_table_name IN ('resume_replacement','resume_replacement_rollout_assignment',
                 'resume_media_isolation_issue','phase11_migration_ledger',
                 'phase11_resume_media_key_scan','phase11_resume_lifecycle_backup')
              OR (table_name='resume' AND column_name IN
                 ('activated_at','candidate_expires_at','delist_reason')))""")).scalar_one())
        trigger_rows = conn.execute(text("""SELECT EVENT_OBJECT_TABLE,ACTION_STATEMENT
          FROM information_schema.triggers WHERE trigger_schema=DATABASE()
            AND event_object_table IN ('resume','resume_replacement',
              'resume_replacement_rollout_assignment','resume_media_isolation_issue',
              'phase11_migration_ledger','phase11_resume_media_key_scan',
              'phase11_resume_lifecycle_backup')""")).all()
        checks = conn.execute(text("""SELECT tc.TABLE_NAME,cc.CHECK_CLAUSE
          FROM information_schema.table_constraints tc
          JOIN information_schema.check_constraints cc
            ON cc.CONSTRAINT_SCHEMA=tc.CONSTRAINT_SCHEMA
           AND cc.CONSTRAINT_NAME=tc.CONSTRAINT_NAME
          WHERE tc.CONSTRAINT_SCHEMA=DATABASE() AND tc.CONSTRAINT_TYPE='CHECK'
            AND tc.TABLE_NAME IN ('resume','resume_replacement',
              'resume_replacement_rollout_assignment','resume_media_isolation_issue',
              'phase11_migration_ledger','phase11_resume_media_key_scan',
              'phase11_resume_lifecycle_backup')""")).all()
    if any(int(value or 0) for value in values.values()):
        raise RuntimeError("down_preconditions_not_met")

    normalized_columns = {
        str(row["COLUMN_NAME"]): (
            str(row["DATA_TYPE"]).lower(), row["DATETIME_PRECISION"], row["IS_NULLABLE"],
            None if row["COLUMN_DEFAULT"] is None else str(row["COLUMN_DEFAULT"]).lower(),
            str(row["EXTRA"] or ""), row["CHARACTER_MAXIMUM_LENGTH"],
        ) for row in actual_columns
    }
    if normalized_columns != expected_resume_columns:
        raise RuntimeError("down_unknown_schema_dependency:resume_column_shape")

    index_map: dict[str, tuple[bool, tuple[str, ...]]] = {}
    grouped: dict[str, list[str]] = {}
    unique: dict[str, bool] = {}
    for row in actual_indexes:
        grouped.setdefault(str(row["INDEX_NAME"]), []).append(str(row["COLUMN_NAME"]))
        unique[str(row["INDEX_NAME"])] = not bool(row["NON_UNIQUE"])
    for name, columns in grouped.items():
        index_map[name] = (unique[name], tuple(columns))
    expected_indexes = {
        "PRIMARY": (True, ("id",)),
        "idx_owner": (False, ("owner_userid",)),
        "idx_audit_time": (False, ("audit_status", "created_at")),
        "idx_expires": (False, ("expires_at",)),
        "idx_resume_candidate_expiry": (False, ("audit_status", "candidate_expires_at")),
        "idx_resume_hard_delete": (False, ("deleted_at", "id")),
        "idx_filter_hot": (
            False, ("gender", "age", "audit_status", "deleted_at", "expires_at"),
        ),
        "idx_salary_exp": (False, ("salary_expect_floor_monthly",)),
    }
    if index_map != expected_indexes:
        raise RuntimeError("down_unknown_schema_dependency:resume_index_shape")

    unknown_prefixed = prefixed_tables - expected_names - down_artifacts
    unknown_triggers = [
        row for row in trigger_rows
        if str(row[0]) != "resume"
        or any(column in str(row[1]).lower() for column in phase_columns)
    ]
    unknown_checks = [
        row for row in checks
        if str(row[0]) != "resume"
        or any(column in str(row[1]).lower() for column in phase_columns)
    ]
    if unknown_prefixed or external_fks or unknown_triggers or unknown_checks:
        raise RuntimeError("down_unknown_schema_dependency")


def run_stage(dsn: str, *, command: str, stage: str, manifest: Path = MANIFEST,
              probes: Iterable[str] = (), cutover_resume_id: int | None = None,
              confirm_down: bool = False, executed_by: str | None = None,
              redis_dsn: str | None = None, redis_namespace: str | None = None,
              _lock_held: bool = False) -> None:
    doc, steps = check_manifest(manifest)
    if stage not in STAGES - {"verify"}:
        raise ValueError("invalid apply stage")
    engine = create_engine(dsn, pool_pre_ping=True, hide_parameters=True)
    if not _lock_held:
        with _migration_lock(engine):
            return run_stage(
                dsn, command=command, stage=stage, manifest=manifest,
                probes=probes, cutover_resume_id=cutover_resume_id,
                confirm_down=confirm_down, executed_by=executed_by,
                redis_dsn=redis_dsn, redis_namespace=redis_namespace,
                _lock_held=True,
            )
    if stage == "down":
        # Validate before even the idempotent ledger bootstrap statement: a
        # downgrade with drifted TTL configuration is strictly read-only.
        _validate_down_ttl_config(engine)
    elif stage == "post_cutover":
        # Fail before ledger bootstrap/attempt mutation when the lifecycle
        # writer could not derive a unique canonical candidate TTL.
        _validate_candidate_ttl_config(engine)
    _bootstrap(engine)
    _validate_ledger(engine, steps)
    build_digest = None
    if stage in {"post_cutover", "down"}:
        build_digest = _probe_builds(doc["minimum_build"], probes)
        if cutover_resume_id is None or cutover_resume_id < 0:
            raise RuntimeError("cutover_resume_id_required")
    candidates = [step for step in steps if step["stage"] == stage]
    if stage == "down":
        if not confirm_down:
            raise RuntimeError("down_confirmation_required")
        # The verified rollout state may have drifted since verify.  Reject a
        # missing, duplicate, non-int, non-canonical or out-of-range TTL
        # before the down ledger, backup tables, Resume rows or schema change.
        _validate_down_ttl_config(engine)
        down_step = candidates[0]
        down_current = _ledger(engine, down_step["key"])
        recovering = (
            command == "resume" and down_current is not None
            and down_current["status"] in {"running", "failed"}
        )
        destructive_progress = recovering and _down_destructive_progress_exists(
            engine, down_step, int(down_current["last_statement_ordinal"] or 0),
        )
        if not destructive_progress:
            _down_preflight(engine)
    with engine.connect() as conn:
        completed = {
            row[0] for row in conn.execute(text(
                "SELECT migration_key FROM phase11_migration_ledger "
                "WHERE status IN ('succeeded','verified')"
            ))
        }
    for step in candidates:
        if not set(step["requires"]) <= completed:
            raise RuntimeError(f"dependency_not_succeeded:{step['key']}")
        current = _ledger(engine, step["key"])
        if current and current["script_sha256"] != step["sha256"]:
            raise RuntimeError(f"ledger_checksum_drift:{step['key']}")
        if current and current["status"] in {"succeeded", "verified"}:
            completed.add(step["key"]); continue
        if command == "apply" and current and current["status"] in {"running", "failed"}:
            raise RuntimeError(f"resume_required:{step['key']}")
        if command == "resume" and current and current["status"] in {"running", "failed"} and not step["resumable"]:
            raise RuntimeError(f"step_not_resumable:{step['key']}")
        if command == "resume" and current is None:
            continue
        if current and stage in {"post_cutover", "down"}:
            stored_cutover = current["cutover_resume_id"]
            if stored_cutover is None or int(stored_cutover) != cutover_resume_id:
                raise RuntimeError(f"cutover_watermark_drift:{step['key']}")
            if current["build_probe_digest"] != build_digest:
                raise RuntimeError(f"build_probe_drift:{step['key']}")
        _mark_running(engine, step, executed_by=executed_by or getpass.getuser(), build_digest=build_digest, cutover_resume_id=cutover_resume_id)
        current = _ledger(engine, step["key"])
        try:
            python_audit_digest = None
            if step["kind"] == "sql":
                _run_sql(engine, step, int(current["last_statement_ordinal"] or 0))
            elif step["kind"] == "python":
                python_audit_digest = _run_python(
                    engine, dsn, step, current["resume_cursor_json"],
                    redis_dsn=redis_dsn, redis_namespace=redis_namespace,
                )
            else:
                raise RuntimeError("verify_sql_is_verify_only")
            _mark_succeeded(engine, step["key"])
            if step["kind"] == "python" and not python_audit_digest:
                raise RuntimeError("python_step_audit_summary_missing")
            completed.add(step["key"])
        except Exception as exc:
            _mark_failed(engine, step["key"], getattr(exc, "args", ["execution_failed"])[0] or "execution_failed")
            raise


def verify(dsn: str, *, manifest: Path = MANIFEST, executed_by: str | None = None,
           _lock_held: bool = False) -> str:
    _, steps = check_manifest(manifest)
    engine = create_engine(dsn, pool_pre_ping=True, hide_parameters=True)
    if not _lock_held:
        with _migration_lock(engine):
            return verify(
                dsn, manifest=manifest, executed_by=executed_by,
                _lock_held=True,
            )
    _bootstrap(engine)
    _validate_ledger(engine, steps)
    required = [step for step in steps if step["stage"] in {"pre_cutover", "post_cutover"}]
    for step in required:
        row = _ledger(engine, step["key"])
        if not row or row["status"] != "succeeded" or row["script_sha256"] != step["sha256"]:
            raise RuntimeError(f"verify_prerequisite_missing:{step['key']}")
    verify_step = next(step for step in steps if step["stage"] == "verify" and step["kind"] == "verify_sql")
    verify_sql = verify_step["file"].read_text(encoding="utf-8")
    # Each summary is its own completed read transaction.  Reusing one
    # REPEATABLE READ connection would merely read the same snapshot twice and
    # could not detect a writer racing verification.
    with engine.begin() as conn:
        _verify_media_registry_projection(conn)
        first = _verify_query(conn, verify_sql)
    _between_verify_snapshots(engine)
    with engine.begin() as conn:
        _verify_media_registry_projection(conn)
        second = _verify_query(conn, verify_sql)
    if first != second:
        raise RuntimeError("verify_summary_changed")
    summary = first or {}
    raw = next(iter(summary.values()), "{}")
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(parsed, dict) or any(int(value or 0) for value in parsed.values()):
        raise RuntimeError("verify_anomalies_present")
    python_audit_digests = {
        step["key"]: str(_ledger(engine, step["key"])["verification_digest"])
        for step in required if step["kind"] == "python"
    }
    # Archive the stable database summary together with the canonical evidence
    # digests produced by every resumable Python backfill/reconciliation step.
    # This makes a later verification digest prove which row/business/status
    # summaries were accepted, rather than attesting only the final snapshot.
    digest = _canonical_digest({
        "verification_summary": parsed,
        "python_audit_digests": python_audit_digests,
    })
    current = _ledger(engine, verify_step["key"])
    if current and (current["script_sha256"] != verify_step["sha256"] or (current["verification_digest"] and current["verification_digest"] != digest)):
        raise RuntimeError("verify_digest_drift")
    if not current:
        _mark_running(engine, verify_step, executed_by=executed_by or getpass.getuser(), build_digest=None, cutover_resume_id=None)
    _mark_succeeded(engine, verify_step["key"], digest=digest)
    return digest


def _between_verify_snapshots(engine) -> None:
    """Test hook at the only intentional gap between verification snapshots."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["manifest-check", "check", "apply", "resume", "verify"])
    parser.add_argument("--stage", choices=sorted(STAGES - {"verify"}), default="pre_cutover")
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--dsn", default=os.getenv("PHASE11_DSN"))
    parser.add_argument("--build-probe-url", action="append", default=[])
    parser.add_argument("--cutover-resume-id", type=int)
    parser.add_argument("--confirm-down", action="store_true")
    parser.add_argument("--executed-by")
    parser.add_argument("--redis-dsn", default=os.getenv("PHASE11_REDIS_DSN"))
    parser.add_argument("--redis-namespace", default=os.getenv("PHASE11_REDIS_NAMESPACE"))
    args = parser.parse_args(argv)
    doc, steps = check_manifest(args.manifest)
    if args.command == "manifest-check":
        print(json.dumps({"status": "ok", "steps": len(steps)})); return 0
    if not args.dsn:
        parser.error("--dsn or PHASE11_DSN is required")
    engine = create_engine(args.dsn, pool_pre_ping=True, hide_parameters=True)
    with engine.connect() as conn: ensure_mysql8(conn)
    if args.command == "check":
        # ``check`` is deliberately read-only. The ledger is created only by
        # apply/resume/verify, never by a preflight invocation.
        ledger = _validate_ledger(engine, steps) if _ledger_table_exists(engine) else {}
        if args.stage in {"post_cutover", "down"}:
            _probe_builds(doc["minimum_build"], args.build_probe_url)
            if args.stage == "post_cutover" and (not args.redis_dsn or not args.redis_namespace):
                raise RuntimeError("orphan_reconcile_requires_explicit_redis_target")
            if args.stage == "post_cutover":
                _validate_candidate_ttl_config(engine)
            required = {
                dependency
                for step in steps if step["stage"] == args.stage
                for dependency in step["requires"]
            }
            missing = sorted(
                key for key in required
                if key not in ledger or ledger[key]["status"] not in {"succeeded", "verified"}
            )
            if missing:
                raise RuntimeError("stage_prerequisite_missing:" + ",".join(missing))
        print(json.dumps({"status": "ok", "stage": args.stage})); return 0
    if args.command == "verify":
        print(json.dumps({"status": "verified", "digest": verify(args.dsn, manifest=args.manifest, executed_by=args.executed_by)})); return 0
    run_stage(args.dsn, command=args.command, stage=args.stage, manifest=args.manifest,
              probes=args.build_probe_url, cutover_resume_id=args.cutover_resume_id,
              confirm_down=args.confirm_down, executed_by=args.executed_by,
              redis_dsn=args.redis_dsn, redis_namespace=args.redis_namespace)
    print(json.dumps({"status": "succeeded", "stage": args.stage})); return 0


if __name__ == "__main__":
    raise SystemExit(run_safely(main, "phase11_migration_runner_failed"))
