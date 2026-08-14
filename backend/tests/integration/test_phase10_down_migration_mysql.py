"""Destructive Phase 10 down migration guards on an isolated real MySQL schema."""
from __future__ import annotations

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import time
from uuid import uuid4

import pymysql
import pytest
from pymysql.constants import CLIENT

from app.db import engine


pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
UP_SQL = (ROOT / "sql/migrations/phase10_001_job_lifecycle_additive.sql").read_text(
    encoding="utf-8"
)
DOWN_SQL = (ROOT / "sql/migrations/phase10_down_001_job_lifecycle.sql").read_text(
    encoding="utf-8"
)

BASE_SCHEMA_SQL = """
CREATE TABLE system_config (
  config_key VARCHAR(64) PRIMARY KEY,
  config_value TEXT NOT NULL
);
INSERT INTO system_config VALUES ('ttl.job.candidate.days','7');
CREATE TABLE job (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  audit_status ENUM('pending','passed','rejected') NOT NULL,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  audited_at DATETIME NULL,
  expires_at DATETIME NOT NULL,
  deleted_at DATETIME NULL,
  delist_reason ENUM('filled','manual_delist','expired') NULL,
  version INT UNSIGNED NOT NULL DEFAULT 1
) ENGINE=InnoDB;
INSERT INTO job (
  audit_status, created_at, updated_at, audited_at, expires_at,
  deleted_at, delist_reason, version
) VALUES
('passed','2026-01-01','2026-01-02','2026-01-03','2026-09-01',NULL,NULL,1),
('pending','2026-01-01','2026-01-02',NULL,'2026-09-01',NULL,NULL,4);
"""


def _connect(database: str | None = None):
    url = engine.url
    return pymysql.connect(
        host=url.host or "127.0.0.1",
        port=int(url.port or 3306),
        user=url.username,
        password=url.password,
        database=database,
        charset="utf8mb4",
        autocommit=True,
        client_flag=CLIENT.MULTI_STATEMENTS,
    )


def _execute_script(connection, sql: str) -> None:
    delimiter = ";"
    statement_lines: list[str] = []
    with connection.cursor() as cursor:
        for line in sql.splitlines(keepends=True):
            stripped = line.strip()
            if stripped.upper().startswith("DELIMITER "):
                if "".join(statement_lines).strip():
                    raise AssertionError("DELIMITER changed inside a SQL statement")
                delimiter = stripped.split(maxsplit=1)[1]
                continue
            statement_lines.append(line)
            if not "".join(statement_lines).rstrip().endswith(delimiter):
                continue
            statement = "".join(statement_lines).rstrip()
            statement = statement[: -len(delimiter)].strip()
            statement_lines.clear()
            if statement:
                cursor.execute(statement)
        if "".join(statement_lines).strip():
            raise AssertionError("unterminated SQL statement")


def _archive_down_evidence(connection) -> tuple[int, int, int]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*), "
            "COALESCE(BIT_XOR(CRC32(CONCAT_WS('|', job_id, audit_status, "
            "COALESCE(expires_at, ''), COALESCE(deleted_at, ''), "
            "COALESCE(delist_reason, ''), version))), 0), "
            "COALESCE(BIT_XOR(CRC32(CONCAT_WS('|', job_id, expected_audit_status, "
            "COALESCE(expected_expires_at, ''), COALESCE(expected_deleted_at, ''), "
            "COALESCE(expected_delist_reason, ''), expected_version, "
            "COALESCE(expected_activated_at, ''), "
            "COALESCE(expected_candidate_expires_at, '')))), 0) "
            "FROM phase10_job_lifecycle_backup"
        )
        values = cursor.fetchone()
    return tuple(int(value) for value in values)


def _set_down_evidence(connection, evidence: tuple[int, int, int]) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SET @phase10_archived_backup_rows=%s, "
            "@phase10_archived_backup_checksum=%s, "
            "@phase10_archived_expected_live_checksum=%s",
            evidence,
        )


def test_down_rejects_post_migration_extension_before_overwrite():
    database = f"phase10_down_{uuid4().hex[:16]}"
    assert database.startswith("phase10_down_")
    admin = _connect()
    db = None
    try:
        with admin.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4")
        db = _connect(database)
        _execute_script(db, BASE_SCHEMA_SQL)
        _execute_script(db, UP_SQL)
        evidence = _archive_down_evidence(db)

        with db.cursor() as cursor:
            cursor.execute(
                "UPDATE job SET expires_at='2026-10-01', version=version+1 WHERE id=1"
            )

        _set_down_evidence(db, evidence)
        with pytest.raises(pymysql.err.ProgrammingError) as exc_info:
            _execute_script(db, DOWN_SQL)
        assert "phase10_down_guard_failed_new_model_data_exists" in str(exc_info.value)
        db.rollback()

        with db.cursor() as cursor:
            cursor.execute("SELECT expires_at, version FROM job WHERE id=1")
            expires_at, version = cursor.fetchone()
            assert str(expires_at) == "2026-10-01 00:00:00"
            assert int(version) == 3
            cursor.execute(
                "SELECT COUNT(*) FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='job' "
                "AND COLUMN_NAME IN ('activated_at','candidate_expires_at')"
            )
            assert int(cursor.fetchone()[0]) == 2

            cursor.execute(
                "UPDATE phase10_job_lifecycle_backup b JOIN job j ON j.id=b.job_id "
                "SET b.expected_expires_at=j.expires_at, "
                "b.expected_version=j.version WHERE b.job_id=1"
            )

        _set_down_evidence(db, evidence)
        with pytest.raises(pymysql.err.ProgrammingError) as exc_info:
            _execute_script(db, DOWN_SQL)
        assert "phase10_down_guard_failed_new_model_data_exists" in str(exc_info.value)
        db.rollback()

        with db.cursor() as cursor:
            cursor.execute(
                "UPDATE phase10_job_lifecycle_backup "
                "SET expected_expires_at='2026-09-01', expected_version=2 "
                "WHERE job_id=1"
            )

            cursor.execute(
                "UPDATE job j JOIN phase10_job_lifecycle_backup b ON b.job_id=j.id "
                "SET j.audit_status=b.expected_audit_status, "
                "j.expires_at=b.expected_expires_at, "
                "j.deleted_at=b.expected_deleted_at, "
                "j.delist_reason=b.expected_delist_reason, "
                "j.version=b.expected_version, "
                "j.activated_at=b.expected_activated_at, "
                "j.candidate_expires_at=b.expected_candidate_expires_at"
            )

        _set_down_evidence(db, evidence)
        _execute_script(db, DOWN_SQL)

        with db.cursor() as cursor:
            cursor.execute("SELECT id, expires_at, version FROM job ORDER BY id")
            assert [(row[0], str(row[1]), row[2]) for row in cursor.fetchall()] == [
                (1, "2026-09-01 00:00:00", 1),
                (2, "2026-09-01 00:00:00", 4),
            ]
            cursor.execute(
                "SELECT COUNT(*) FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() "
                "AND TABLE_NAME='phase10_job_lifecycle_backup' "
                "AND COLUMN_NAME LIKE 'expected_%'"
            )
            assert int(cursor.fetchone()[0]) == 0
    finally:
        if db is not None:
            db.close()
        with admin.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")
        admin.close()


def test_down_fence_rejects_concurrent_job_and_empty_model_writes():
    database = f"phase10_down_{uuid4().hex[:16]}"
    admin = _connect()
    setup = down = concurrent = None
    try:
        with admin.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4")
        setup = _connect(database)
        _execute_script(setup, BASE_SCHEMA_SQL)
        _execute_script(setup, UP_SQL)
        evidence = _archive_down_evidence(setup)

        down = _connect(database)
        concurrent = _connect(database)
        _set_down_evidence(down, evidence)
        delayed_down_sql = DOWN_SQL.replace(
            "COMMIT;\n\nALTER TABLE `job` DROP INDEX",
            "COMMIT;\nSELECT SLEEP(2);\n\nALTER TABLE `job` DROP INDEX",
            1,
        )
        assert delayed_down_sql != DOWN_SQL

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_execute_script, down, delayed_down_sql)
            with concurrent.cursor() as cursor:
                deadline = time.monotonic() + 5
                while True:
                    cursor.execute(
                        "SELECT writes_blocked "
                        "FROM phase10_migration_control WHERE id=1"
                    )
                    if int(cursor.fetchone()[0]) == 1:
                        break
                    if time.monotonic() >= deadline:
                        raise AssertionError("down migration fence was not activated")
                    time.sleep(0.02)

                with pytest.raises(pymysql.err.OperationalError) as update_error:
                    cursor.execute("UPDATE job SET version=version+1 WHERE id=1")
                assert "phase10_destructive_down_in_progress" in str(update_error.value)

                with pytest.raises(pymysql.err.OperationalError) as insert_error:
                    cursor.execute(
                        "INSERT INTO target_cleanup_task "
                        "(operation_id, target_type, target_id, reason) "
                        "VALUES (UUID(), 'job', 1, 'test')"
                    )
                assert "phase10_destructive_down_in_progress" in str(insert_error.value)
            future.result(timeout=10)

        with setup.cursor() as cursor:
            cursor.execute("SELECT version FROM job WHERE id=1")
            assert int(cursor.fetchone()[0]) == 1
            cursor.execute(
                "SELECT COUNT(*) FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='target_cleanup_task'"
            )
            assert int(cursor.fetchone()[0]) == 0
    finally:
        for connection in (setup, down, concurrent):
            if connection is not None:
                connection.close()
        with admin.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")
        admin.close()


def test_empty_job_table_round_trip_uses_zero_checksums():
    database = f"phase10_down_{uuid4().hex[:16]}"
    admin = _connect()
    db = None
    try:
        with admin.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4")
        db = _connect(database)
        _execute_script(db, BASE_SCHEMA_SQL)
        with db.cursor() as cursor:
            cursor.execute("DELETE FROM job")

        _execute_script(db, UP_SQL)
        evidence = _archive_down_evidence(db)
        assert evidence == (0, 0, 0)

        _set_down_evidence(db, evidence)
        _execute_script(db, DOWN_SQL)

        with db.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM job")
            assert int(cursor.fetchone()[0]) == 0
            cursor.execute(
                "SELECT COUNT(*) FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='job' "
                "AND COLUMN_NAME IN ('activated_at','candidate_expires_at')"
            )
            assert int(cursor.fetchone()[0]) == 0
    finally:
        if db is not None:
            db.close()
        with admin.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")
        admin.close()
