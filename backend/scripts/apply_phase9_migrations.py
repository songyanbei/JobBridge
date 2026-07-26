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


def run(dsn_env: str, manifest_file: str, mode: str) -> None:
    root = Path(__file__).resolve().parents[1] / "sql" / "migrations"
    manifest = root / Path(manifest_file).name
    entries = check_files(root, manifest)
    if mode == "check":
        print(f"phase9 checksum ok: {len(entries)} migrations")
        return
    engine = create_engine(dsn_from_env(dsn_env), pool_pre_ping=True)
    with engine.begin() as conn:
        conn.execute(text(LEDGER_DDL))
        existing = {
            row[0]: row[1]
            for row in conn.execute(text(
                "SELECT migration_name, sha256 FROM schema_migration_history "
                "WHERE migration_name LIKE 'phase9_%'"
            ))
        }
        if mode == "verify":
            missing = [name for name, digest in entries if existing.get(name) != digest]
            if missing:
                raise SystemExit(f"phase9 verification failed: {missing}")
            print("phase9 verification ok")
            return
        for filename, digest in entries:
            if filename in existing:
                if existing[filename] != digest:
                    raise SystemExit(f"database checksum mismatch: {filename}")
                continue
            started = time.monotonic()
            sql = (root / filename).read_text(encoding="utf-8")
            for statement in (part.strip() for part in sql.split(";")):
                if statement:
                    conn.execute(text(statement))
            elapsed = int((time.monotonic() - started) * 1000)
            conn.execute(text(
                "INSERT INTO schema_migration_history "
                "(migration_name, sha256, applied_by, duration_ms, success, server_version) "
                "VALUES (:name, :digest, :user, :duration, 1, VERSION())"
            ), {"name": filename, "digest": digest, "user": os.environ.get("USERNAME", "unknown"), "duration": elapsed})
            print(f"applied {filename} ({elapsed}ms)")


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
