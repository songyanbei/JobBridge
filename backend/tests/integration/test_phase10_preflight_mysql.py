"""Phase 10 preflight checks that require a real MySQL database."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pymysql
import pytest
from pymysql.constants import CLIENT
from sqlalchemy import text

from app.db import SessionLocal, engine
from scripts.phase10_clock_check import collect_clock_report
from scripts.phase10_preflight import CHECKS, collect_redis_policy


pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
UP_SQL = (ROOT / "sql/migrations/phase10_001_job_lifecycle_additive.sql").read_text(
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
('pending','2026-01-04','2026-01-05',NULL,'2026-09-02',NULL,NULL,1),
('rejected','2026-01-06','2026-01-07','2026-01-08','2026-09-03',
 '2026-02-01','expired',1);
CREATE TABLE wecom_inbound_event (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  status ENUM('received','processing','session_pending','done','failed','dead_letter')
    NOT NULL DEFAULT 'received',
  session_apply_locked_at DATETIME(6) NULL,
  session_next_attempt_at DATETIME(6) NULL,
  worker_started_at DATETIME(6) NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB;
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


def test_additive_migration_uses_utc_for_candidate_deadlines():
    database = f"phase10_utc_{uuid4().hex[:16]}"
    admin = _connect()
    db = None
    try:
        with admin.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4")
        db = _connect(database)
        _execute_script(db, BASE_SCHEMA_SQL)

        with db.cursor() as cursor:
            cursor.execute(
                "INSERT INTO job ("
                "audit_status, created_at, updated_at, audited_at, expires_at, "
                "deleted_at, delist_reason, version"
                ") VALUES ("
                "'rejected','2026-01-09','2026-01-10','2026-01-11',"
                "'2026-09-04',NULL,NULL,1)"
            )
            cursor.execute("SET time_zone = '+08:00'")
            cursor.execute(
                "SELECT UTC_TIMESTAMP(), "
                "TIMESTAMPDIFF(HOUR, UTC_TIMESTAMP(), NOW())"
            )
            utc_before, timezone_offset_hours = cursor.fetchone()
        assert int(timezone_offset_hours) == 8

        _execute_script(db, UP_SQL)

        with db.cursor() as cursor:
            cursor.execute("SELECT UTC_TIMESTAMP()")
            utc_after = cursor.fetchone()[0]
            cursor.execute(
                "SELECT audit_status, candidate_expires_at FROM job "
                "WHERE deleted_at IS NULL "
                "AND audit_status IN ('pending','rejected') "
                "ORDER BY audit_status"
            )
            candidate_deadlines = cursor.fetchall()

        expected_before = utc_before + timedelta(days=7)
        expected_after = utc_after + timedelta(days=7)
        assert [row[0] for row in candidate_deadlines] == ["pending", "rejected"]
        for _, candidate_deadline in candidate_deadlines:
            assert expected_before <= candidate_deadline <= expected_after
    finally:
        if db is not None:
            db.close()
        with admin.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")
        admin.close()


def test_mysql_and_redis_clock_skew_is_within_rollout_limit():
    with SessionLocal() as db:
        report = collect_clock_report(db)

    assert report["clock_skew_seconds"] <= 2.0
    assert report["ready"] is True


def test_session_schema_and_live_redis_rollout_gates_pass():
    db = SessionLocal()
    try:
        for name in (
            "session_commit_deadline_schema_mismatch",
            "session_apply_lease_owner_schema_mismatch",
            "session_commit_due_index_mismatch",
        ):
            assert int(db.execute(text(CHECKS[name])).scalar_one()) == 0, name
    finally:
        db.close()

    redis_policy = collect_redis_policy()
    assert redis_policy == {
        "redis_durability_policy_mismatch": 0,
        "redis_maxmemory_policy": "noeviction",
        "redis_appendonly": "yes",
        "redis_appendfsync": "always",
    }


def test_job_ttl_preflight_accepts_full_supported_range():
    db = SessionLocal()
    try:
        db.execute(
            text(
                "INSERT INTO system_config "
                "(config_key, config_value, value_type, description) "
                "VALUES ('ttl.job.days', '30', 'int', 'integration test') "
                "ON DUPLICATE KEY UPDATE config_value=VALUES(config_value)"
            )
        )
        for value, expected in (
            ("365", 0),
            ("366", 0),
            ("3650", 0),
            ("0", 1),
            ("3651", 1),
            ("invalid", 1),
        ):
            db.execute(
                text(
                    "UPDATE system_config SET config_value=:value "
                    "WHERE config_key='ttl.job.days'"
                ),
                {"value": value},
            )
            result = db.execute(text(CHECKS["invalid_job_ttl_config"])).scalar_one()
            assert int(result) == expected, value
    finally:
        db.rollback()
        db.close()


def test_backup_integrity_gate_ignores_live_job_additions_and_hard_deletes():
    database = f"phase10_preflight_{uuid4().hex[:16]}"
    admin = _connect()
    db = None
    try:
        with admin.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4")
        db = _connect(database)
        _execute_script(db, BASE_SCHEMA_SQL)
        _execute_script(db, UP_SQL)

        with db.cursor() as cursor:
            gate = CHECKS["job_backup_coverage_mismatch"]
            cursor.execute(gate)
            assert int(cursor.fetchone()[0]) == 0

            cursor.execute(
                "INSERT INTO job (audit_status, created_at, updated_at, audited_at, "
                "activated_at, candidate_expires_at, expires_at, deleted_at, "
                "delist_reason, version) VALUES "
                "('passed','2026-02-01','2026-02-02','2026-02-03',"
                "'2026-02-03',NULL,'2026-10-01',NULL,NULL,1)"
            )
            cursor.execute(gate)
            assert int(cursor.fetchone()[0]) == 0

            cursor.execute("DELETE FROM job WHERE id=1")
            cursor.execute(gate)
            assert int(cursor.fetchone()[0]) == 0

            cursor.execute(
                "UPDATE phase10_job_lifecycle_backup "
                "SET expected_version=expected_version+1 WHERE job_id=1"
            )
            cursor.execute(gate)
            assert int(cursor.fetchone()[0]) == 1

            cursor.execute(
                "UPDATE phase10_job_lifecycle_backup "
                "SET expected_version=expected_version-1 WHERE job_id=1"
            )
            cursor.execute(gate)
            assert int(cursor.fetchone()[0]) == 0

            cursor.execute(
                "UPDATE phase10_migration_control "
                "SET expected_live_checksum=expected_live_checksum+1 WHERE id=1"
            )
            cursor.execute(gate)
            assert int(cursor.fetchone()[0]) == 1

            cursor.execute(
                "UPDATE phase10_migration_control "
                "SET expected_live_checksum=expected_live_checksum-1 WHERE id=1"
            )
            cursor.execute(gate)
            assert int(cursor.fetchone()[0]) == 0

            cursor.execute(
                "UPDATE phase10_migration_control "
                "SET source_candidate_rows=source_candidate_rows+1 WHERE id=1"
            )
            cursor.execute(gate)
            assert int(cursor.fetchone()[0]) == 1

            cursor.execute(
                "UPDATE phase10_migration_control "
                "SET source_candidate_rows=source_candidate_rows-1 WHERE id=1"
            )
            cursor.execute(gate)
            assert int(cursor.fetchone()[0]) == 0

            cursor.execute("DELETE FROM phase10_migration_control WHERE id=1")
            cursor.execute(gate)
            assert int(cursor.fetchone()[0]) == 1
    finally:
        if db is not None:
            db.close()
        with admin.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")
        admin.close()


def test_migration_gates_classification_counts_and_live_checksum():
    database = f"phase10_evidence_{uuid4().hex[:16]}"
    drift_database = f"phase10_evidence_drift_{uuid4().hex[:16]}"
    checksum_database = f"phase10_evidence_checksum_{uuid4().hex[:16]}"
    admin = _connect()
    db = None
    drift_db = None
    checksum_db = None
    try:
        with admin.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4")
            cursor.execute(
                f"CREATE DATABASE `{drift_database}` CHARACTER SET utf8mb4"
            )
            cursor.execute(
                f"CREATE DATABASE `{checksum_database}` CHARACTER SET utf8mb4"
            )

        db = _connect(database)
        _execute_script(db, BASE_SCHEMA_SQL)
        _execute_script(db, UP_SQL)
        with db.cursor() as cursor:
            cursor.execute(
                "SELECT c.source_soft_deleted_rows, "
                "c.source_passed_online_rows, c.source_candidate_rows, "
                "(SELECT COUNT(*) FROM job WHERE deleted_at IS NOT NULL), "
                "(SELECT COUNT(*) FROM job WHERE deleted_at IS NULL "
                " AND audit_status='passed'), "
                "(SELECT COUNT(*) FROM job WHERE deleted_at IS NULL "
                " AND audit_status IN ('pending','rejected')), "
                "c.expected_live_checksum, "
                "(SELECT COALESCE(BIT_XOR(CRC32(CONCAT_WS('|', id, audit_status, "
                " COALESCE(expires_at, ''), COALESCE(deleted_at, ''), "
                " COALESCE(delist_reason, ''), version, COALESCE(activated_at, ''), "
                " COALESCE(candidate_expires_at, '')))), 0) FROM job) "
                "FROM phase10_migration_control c WHERE c.id=1"
            )
            evidence = tuple(int(value) for value in cursor.fetchone())
        assert evidence[:6] == (1, 1, 1, 1, 1, 1)
        assert evidence[6] == evidence[7]

        drift_db = _connect(drift_database)
        _execute_script(drift_db, BASE_SCHEMA_SQL)
        assertion = "CALL `phase10_assert_lifecycle_backfill`();"
        assert assertion in UP_SQL
        drift_sql = UP_SQL.replace(
            assertion,
            "UPDATE `job` SET `audit_status`='pending', `activated_at`=NULL, "
            "`expires_at`=NULL, `candidate_expires_at`=DATE_ADD("
            "@phase10_migration_time, INTERVAL @phase10_candidate_days DAY) "
            "WHERE `id`=1;\n" + assertion,
            1,
        )
        with pytest.raises(
            pymysql.MySQLError,
            match="phase10_lifecycle_backfill_evidence_mismatch",
        ):
            _execute_script(drift_db, drift_sql)

        checksum_db = _connect(checksum_database)
        _execute_script(checksum_db, BASE_SCHEMA_SQL)
        passed_backfill = (
            "SET `activated_at` = COALESCE(`audited_at`, `created_at`),\n"
            "    `version` = `version` + 1\n"
            "WHERE `deleted_at` IS NULL AND `audit_status` = 'passed';"
        )
        assert passed_backfill in UP_SQL
        invalid_checksum_sql = UP_SQL.replace(
            passed_backfill,
            "SET `activated_at` = DATE_ADD(COALESCE(`audited_at`, `created_at`), "
            "INTERVAL 1 SECOND),\n"
            "    `version` = `version` + 1\n"
            "WHERE `deleted_at` IS NULL AND `audit_status` = 'passed';",
            1,
        )
        with pytest.raises(
            pymysql.MySQLError,
            match="phase10_lifecycle_backfill_evidence_mismatch",
        ):
            _execute_script(checksum_db, invalid_checksum_sql)
    finally:
        if db is not None:
            db.close()
        if drift_db is not None:
            drift_db.close()
        if checksum_db is not None:
            checksum_db.close()
        with admin.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")
            cursor.execute(f"DROP DATABASE IF EXISTS `{drift_database}`")
            cursor.execute(f"DROP DATABASE IF EXISTS `{checksum_database}`")
        admin.close()
