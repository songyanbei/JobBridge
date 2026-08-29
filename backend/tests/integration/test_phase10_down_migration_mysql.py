"""Destructive Phase 10 down migration guards on an isolated real MySQL schema."""
from __future__ import annotations

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import time
from uuid import uuid4

import pymysql
import pytest
from pymysql.constants import CLIENT
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.db import engine
from scripts.phase10_down_verify import collect as collect_down_report


pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
UP_SQL = (ROOT / "sql/migrations/phase10_001_job_lifecycle_additive.sql").read_text(
    encoding="utf-8"
)
MEDIA_SQL = (ROOT / "sql/migrations/phase10_002_media_dead_letter.sql").read_text(
    encoding="utf-8"
)
DEADLINE_SQL = (
    ROOT / "sql/migrations/phase10_003_session_commit_deadline.sql"
).read_text(encoding="utf-8")
LEASE_OWNER_SQL = (
    ROOT / "sql/migrations/phase10_004_session_commit_lease_owner.sql"
).read_text(encoding="utf-8")
DOWN_SQL = (ROOT / "sql/migrations/phase10_down_001_job_lifecycle.sql").read_text(
    encoding="utf-8"
)
STAGE_A_JOB_SQL = (ROOT / "sql/contracts/phase10_stage_a_job.sql").read_text(
    encoding="utf-8"
)

BASE_SCHEMA_SQL = """
CREATE TABLE system_config (
  config_key VARCHAR(64) PRIMARY KEY,
  config_value TEXT NOT NULL,
  value_type ENUM('string','int','bool','json') NOT NULL DEFAULT 'string',
  description VARCHAR(255) NULL
);
CREATE TABLE `user` (
  external_userid VARCHAR(64) NOT NULL PRIMARY KEY
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
INSERT INTO `user` VALUES ('stage-a-owner');
""" + STAGE_A_JOB_SQL + """
INSERT INTO job (
  owner_userid, city, job_category, salary_floor_monthly, pay_type, headcount,
  raw_text, audit_status, created_at, updated_at, audited_at, expires_at,
  deleted_at, delist_reason, version
) VALUES
('stage-a-owner','上海','普工',8000,'月薪',2,'岗位一','passed','2026-01-01','2026-01-02','2026-01-03','2026-09-01',NULL,NULL,1),
('stage-a-owner','苏州','操作工',7000,'月薪',3,'岗位二','pending','2026-01-01','2026-01-02',NULL,'2026-09-01',NULL,NULL,4);
CREATE TABLE wecom_inbound_event (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  msg_id VARCHAR(64) NOT NULL,
  from_userid VARCHAR(64) NOT NULL,
  msg_type ENUM('text','image','voice','video','file','link','location','event','other')
    NOT NULL,
  media_id VARCHAR(128) NULL,
  content_brief VARCHAR(500) NULL,
  status ENUM('received','processing','session_pending','done','failed','dead_letter')
    NOT NULL DEFAULT 'received',
  retry_count TINYINT UNSIGNED NOT NULL DEFAULT 0,
  session_operation VARCHAR(8) NULL,
  session_expected_version INT UNSIGNED NULL,
  session_payload JSON NULL,
  session_apply_attempts INT UNSIGNED NOT NULL DEFAULT 0,
  session_apply_locked_at DATETIME(6) NULL,
  session_next_attempt_at DATETIME(6) NULL,
  session_applied_at DATETIME(6) NULL,
  worker_started_at DATETIME(6) NULL,
  worker_finished_at DATETIME(6) NULL,
  error_message TEXT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  UNIQUE KEY uk_msg_id (msg_id),
  KEY idx_status_time (status, created_at),
  KEY idx_status_worker_started (status, worker_started_at),
  KEY idx_status_worker_finished (status, worker_finished_at),
  KEY idx_from_user (from_userid, created_at),
  KEY idx_user_status_id (from_userid, status, id),
  KEY idx_session_commit_due (
    status, session_next_attempt_at, session_apply_locked_at, id
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
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


def _execute_phase10_up(connection) -> None:
    for sql in (UP_SQL, MEDIA_SQL, DEADLINE_SQL, LEASE_OWNER_SQL):
        _execute_script(connection, sql)


def _archive_down_evidence(connection) -> tuple[int, int, int]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*), "
            "COALESCE(BIT_XOR(CRC32(CONCAT_WS('|', job_id, audit_status, "
            "COALESCE(expires_at, ''), COALESCE(deleted_at, ''), "
            "COALESCE(delist_reason, ''), version, source_updated_at))), 0), "
            "COALESCE(BIT_XOR(CRC32(CONCAT_WS('|', job_id, expected_audit_status, "
            "COALESCE(expected_expires_at, ''), COALESCE(expected_deleted_at, ''), "
            "COALESCE(expected_delist_reason, ''), expected_version, "
            "expected_updated_at, "
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


def _collect_down_report(database: str) -> dict[str, int | bool]:
    verify_engine = create_engine(engine.url.set(database=database))
    try:
        with sessionmaker(bind=verify_engine)() as verify_session:
            account = str(
                verify_session.execute(text("SELECT CURRENT_USER()")).scalar()
            )
            return collect_down_report(
                verify_session, expected_account=account
            )
    finally:
        verify_engine.dispose()


def _collect_down_report_as(
    database: str, username: str, password: str
) -> dict[str, int | bool]:
    verify_engine = create_engine(
        engine.url.set(database=database, username=username, password=password)
    )
    try:
        with sessionmaker(bind=verify_engine)() as verify_session:
            account = str(
                verify_session.execute(text("SELECT CURRENT_USER()")).scalar()
            )
            return collect_down_report(
                verify_session, expected_account=account
            )
    finally:
        verify_engine.dispose()


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
        _execute_phase10_up(db)
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
                "j.updated_at=b.expected_updated_at, "
                "j.activated_at=b.expected_activated_at, "
                "j.candidate_expires_at=b.expected_candidate_expires_at"
            )

        _set_down_evidence(db, evidence)
        _execute_script(db, DOWN_SQL)

        with db.cursor() as cursor:
            cursor.execute(
                "SELECT id, expires_at, version, updated_at FROM job ORDER BY id"
            )
            assert [
                (row[0], str(row[1]), row[2], str(row[3]))
                for row in cursor.fetchall()
            ] == [
                (1, "2026-09-01 00:00:00", 1, "2026-01-02 00:00:00"),
                (2, "2026-09-01 00:00:00", 4, "2026-01-02 00:00:00"),
            ]
            cursor.execute(
                "SELECT COUNT(*) FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() "
                "AND TABLE_NAME='phase10_job_lifecycle_backup' "
                "AND COLUMN_NAME LIKE 'expected_%'"
            )
            assert int(cursor.fetchone()[0]) == 0

        report = _collect_down_report(database)
        assert report["ready"] is True, report
        assert report["restored_job_backup_mismatch"] == 0

        with db.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE phase10_job_lifecycle_backup DROP PRIMARY KEY"
            )
            cursor.execute(
                "INSERT INTO phase10_job_lifecycle_backup "
                "SELECT * FROM phase10_job_lifecycle_backup WHERE job_id=1"
            )
        report = _collect_down_report(database)
        assert report["restored_job_backup_mismatch"] == 0
        assert report["backup_job_id_key_contract_mismatch"] == 1
        assert report["backup_duplicate_job_id_rows"] == 1
        assert report["restored_job_backup_row_count_mismatch"] == 1
        assert report["ready"] is False
        with db.cursor() as cursor:
            cursor.execute(
                "DELETE FROM phase10_job_lifecycle_backup "
                "WHERE job_id=1 LIMIT 1"
            )
            cursor.execute(
                "ALTER TABLE phase10_job_lifecycle_backup "
                "ADD PRIMARY KEY (job_id)"
            )

        with db.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND BINARY TABLE_NAME='job'"
            )
            assert int(cursor.fetchone()[0]) == 48

            cursor.execute("ALTER TABLE job DROP COLUMN description")
        report = _collect_down_report(database)
        assert report["old_job_column_contract_mismatch"] == 1
        assert report["ready"] is False
        with db.cursor() as cursor:
            cursor.execute("ALTER TABLE job ADD COLUMN description TEXT NULL")
            cursor.execute("ALTER TABLE job MODIFY COLUMN expires_at DATE NOT NULL")
        report = _collect_down_report(database)
        assert report["old_job_column_contract_mismatch"] == 1
        assert report["ready"] is False
        with db.cursor() as cursor:
            cursor.execute("ALTER TABLE job MODIFY COLUMN expires_at DATETIME NOT NULL")
            cursor.execute(
                "ALTER TABLE job ADD CONSTRAINT chk_job_stage_a "
                "CHECK (headcount <> 99)"
            )
        report = _collect_down_report(database)
        assert report["old_job_constraints_mismatch"] == 1
        assert report["ready"] is False
        with db.cursor() as cursor:
            cursor.execute("ALTER TABLE job DROP CHECK chk_job_stage_a")
            cursor.execute(
                "ALTER TABLE `user` ADD COLUMN is_long_term TINYINT(1) NOT NULL "
                "DEFAULT 1, ADD UNIQUE KEY uq_user_owner_long_term "
                "(external_userid, is_long_term)"
            )
            cursor.execute("ALTER TABLE job DROP FOREIGN KEY fk_job_owner")
            cursor.execute(
                "ALTER TABLE job ADD KEY idx_job_owner_long_term "
                "(owner_userid, is_long_term), "
                "ADD CONSTRAINT fk_job_owner "
                "FOREIGN KEY (owner_userid, is_long_term) "
                "REFERENCES `user` (external_userid, is_long_term) "
                "ON DELETE RESTRICT"
            )
        report = _collect_down_report(database)
        assert report["old_job_constraints_mismatch"] == 1
        assert report["ready"] is False
        with db.cursor() as cursor:
            cursor.execute("ALTER TABLE job DROP FOREIGN KEY fk_job_owner")
            cursor.execute("ALTER TABLE job DROP INDEX idx_job_owner_long_term")
            cursor.execute(
                "ALTER TABLE job ADD CONSTRAINT fk_job_owner "
                "FOREIGN KEY (owner_userid) REFERENCES `user` (external_userid) "
                "ON DELETE RESTRICT"
            )
            cursor.execute(
                "CREATE TRIGGER job_stage_a_block BEFORE INSERT ON job "
                "FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='blocked'"
            )
        report = _collect_down_report(database)
        assert report["old_job_triggers_remaining"] == 1
        assert report["ready"] is False
        with db.cursor() as cursor:
            cursor.execute("DROP TRIGGER job_stage_a_block")
            cursor.execute("CREATE INDEX idx_job_extra ON job (headcount)")
        report = _collect_down_report(database)
        assert report["old_job_index_contract_mismatch"] == 1
        assert report["ready"] is False
        with db.cursor() as cursor:
            cursor.execute("DROP INDEX idx_job_extra ON job")
            cursor.execute(
                "ALTER TABLE job DEFAULT CHARACTER SET utf8mb4 "
                "COLLATE utf8mb4_general_ci"
            )
        report = _collect_down_report(database)
        assert report["old_job_table_contract_mismatch"] == 1
        assert report["ready"] is False
        with db.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE job DEFAULT CHARACTER SET utf8mb4 "
                "COLLATE utf8mb4_0900_ai_ci"
            )
            cursor.execute("ALTER TABLE job DROP FOREIGN KEY fk_job_owner")
            cursor.execute("ALTER TABLE job ENGINE=MyISAM")
        report = _collect_down_report(database)
        assert report["old_job_table_contract_mismatch"] == 1
        assert report["ready"] is False
        with db.cursor() as cursor:
            cursor.execute("SELECT version FROM job WHERE id=1")
            version_before = int(cursor.fetchone()[0])
            cursor.execute("START TRANSACTION")
            cursor.execute("UPDATE job SET version=version+1 WHERE id=1")
            cursor.execute("ROLLBACK")
            cursor.execute("SELECT version FROM job WHERE id=1")
            assert int(cursor.fetchone()[0]) == version_before + 1
            cursor.execute("ALTER TABLE job ENGINE=InnoDB")
            cursor.execute(
                "ALTER TABLE job ADD CONSTRAINT fk_job_owner "
                "FOREIGN KEY (owner_userid) REFERENCES `user` (external_userid) "
                "ON DELETE RESTRICT"
            )
    finally:
        if db is not None:
            db.close()
        with admin.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")
        admin.close()


def test_down_verify_fails_closed_without_global_fk_metadata_visibility():
    database = f"phase10_down_{uuid4().hex[:16]}"
    referencing_database = f"phase10_ref_{uuid4().hex[:16]}"
    metadata_user = f"phase10_meta_{uuid4().hex[:12]}"
    metadata_password = uuid4().hex
    admin = _connect()
    db = None
    try:
        with admin.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4")
            cursor.execute(
                f"CREATE DATABASE `{referencing_database}` CHARACTER SET utf8mb4"
            )
        db = _connect(database)
        _execute_script(db, BASE_SCHEMA_SQL)
        _execute_phase10_up(db)
        evidence = _archive_down_evidence(db)
        _set_down_evidence(db, evidence)
        _execute_script(db, DOWN_SQL)

        with db.cursor() as cursor:
            cursor.execute(
                "INSERT INTO wecom_inbound_event "
                "(msg_id, from_userid, msg_type) "
                "VALUES ('cross-schema-fk', 'cross-schema-user', 'text')"
            )
            inbound_id = int(cursor.lastrowid)
            cursor.execute(
                f"CREATE TABLE `{referencing_database}`.outbox_ref ("
                "id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY, "
                "inbound_event_id BIGINT UNSIGNED NOT NULL, "
                "CONSTRAINT fk_cross_schema_inbound "
                "FOREIGN KEY (inbound_event_id) "
                f"REFERENCES `{database}`.wecom_inbound_event(id) "
                "ON DELETE RESTRICT) ENGINE=InnoDB"
            )
            cursor.execute(
                f"INSERT INTO `{referencing_database}`.outbox_ref "
                "(inbound_event_id) VALUES (%s)",
                (inbound_id,),
            )

        privileged_report = _collect_down_report(database)
        assert privileged_report["down_verify_global_select_privilege_missing"] == 0
        assert privileged_report[
            "old_inbound_referencing_foreign_keys_mismatch"
        ] == 1
        assert privileged_report["ready"] is False

        with admin.cursor() as cursor:
            cursor.execute(
                f"CREATE USER `{metadata_user}`@'%%' IDENTIFIED BY %s",
                (metadata_password,),
            )
            cursor.execute(
                f"GRANT SELECT ON `{database}`.* TO `{metadata_user}`@'%'"
            )
        restricted_report = _collect_down_report_as(
            database, metadata_user, metadata_password
        )
        assert restricted_report[
            "old_inbound_referencing_foreign_keys_mismatch"
        ] == 0
        assert restricted_report["down_verify_global_select_privilege_missing"] == 1
        assert restricted_report["ready"] is False

        with db.cursor() as cursor:
            with pytest.raises(pymysql.err.IntegrityError) as delete_error:
                cursor.execute(
                    "DELETE FROM wecom_inbound_event WHERE id=%s", (inbound_id,)
                )
            assert delete_error.value.args[0] == 1451
    finally:
        if db is not None:
            db.close()
        with admin.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{referencing_database}`")
            cursor.execute(f"DROP USER IF EXISTS `{metadata_user}`@'%'")
            cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")
        admin.close()


def test_down_verify_fails_closed_without_trigger_metadata_visibility():
    database = f"phase10_down_{uuid4().hex[:16]}"
    metadata_user = f"phase10_trigger_{uuid4().hex[:10]}"
    metadata_password = uuid4().hex
    admin = _connect()
    db = restricted = None
    try:
        with admin.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4")
        db = _connect(database)
        _execute_script(db, BASE_SCHEMA_SQL)
        _execute_phase10_up(db)
        evidence = _archive_down_evidence(db)
        _set_down_evidence(db, evidence)
        _execute_script(db, DOWN_SQL)

        with db.cursor() as cursor:
            cursor.execute(
                "CREATE TRIGGER hidden_stage_a_inbound "
                "BEFORE INSERT ON wecom_inbound_event FOR EACH ROW "
                "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='hidden trigger'"
            )
        privileged_report = _collect_down_report(database)
        assert privileged_report["down_verify_trigger_privilege_missing"] == 0
        assert privileged_report["old_inbound_triggers_remaining"] == 1

        with admin.cursor() as cursor:
            cursor.execute(
                f"CREATE USER `{metadata_user}`@'%%' IDENTIFIED BY %s",
                (metadata_password,),
            )
            cursor.execute(f"GRANT SELECT ON *.* TO `{metadata_user}`@'%'")
            cursor.execute(
                f"GRANT INSERT ON `{database}`.wecom_inbound_event "
                f"TO `{metadata_user}`@'%'"
            )

        restricted_report = _collect_down_report_as(
            database, metadata_user, metadata_password
        )
        assert restricted_report["down_verify_database_account_mismatch"] == 0
        assert restricted_report["down_verify_global_select_privilege_missing"] == 0
        assert restricted_report["old_inbound_triggers_remaining"] == 0
        assert restricted_report["down_verify_trigger_privilege_missing"] == 1
        assert restricted_report["ready"] is False

        url = engine.url
        restricted = pymysql.connect(
            host=url.host or "127.0.0.1",
            port=int(url.port or 3306),
            user=metadata_user,
            password=metadata_password,
            database=database,
            charset="utf8mb4",
            autocommit=True,
        )
        with restricted.cursor() as cursor:
            with pytest.raises(pymysql.err.OperationalError) as trigger_error:
                cursor.execute(
                    "INSERT INTO wecom_inbound_event "
                    "(msg_id, from_userid, msg_type) "
                    "VALUES ('hidden-trigger-msg', 'hidden-trigger-user', 'text')"
                )
            assert trigger_error.value.args[0] == 1644
    finally:
        if restricted is not None:
            restricted.close()
        if db is not None:
            db.close()
        with admin.cursor() as cursor:
            cursor.execute(f"DROP USER IF EXISTS `{metadata_user}`@'%'")
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
        _execute_phase10_up(setup)
        evidence = _archive_down_evidence(setup)
        with setup.cursor() as cursor:
            cursor.execute(
                "INSERT INTO wecom_inbound_event "
                "(msg_id, from_userid, msg_type, status) "
                "VALUES ('down-fence-msg', 'down-fence-user', 'text', 'received')"
            )
            inbound_event_id = int(cursor.lastrowid)

        down = _connect(database)
        concurrent = _connect(database)
        _set_down_evidence(down, evidence)
        delayed_down_sql = DOWN_SQL.replace(
            "COMMIT;\n\nALTER TABLE `wecom_inbound_event`",
            "COMMIT;\nSELECT SLEEP(2);\n\nALTER TABLE `wecom_inbound_event`",
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

                with pytest.raises(pymysql.err.OperationalError) as inbound_error:
                    cursor.execute(
                        "UPDATE wecom_inbound_event "
                        "SET status='session_pending', "
                        "session_commit_deadline_epoch=UNIX_TIMESTAMP(NOW(6)) + 1800 "
                        "WHERE id=%s",
                        (inbound_event_id,),
                    )
                assert "phase10_destructive_down_in_progress" in str(
                    inbound_error.value
                )
            future.result(timeout=10)

        with setup.cursor() as cursor:
            cursor.execute("SELECT version FROM job WHERE id=1")
            assert int(cursor.fetchone()[0]) == 1
            cursor.execute(
                "SELECT COUNT(*) FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='target_cleanup_task'"
            )
            assert int(cursor.fetchone()[0]) == 0
            cursor.execute(
                "SELECT status FROM wecom_inbound_event WHERE id=%s",
                (inbound_event_id,),
            )
            assert cursor.fetchone()[0] == "received"
    finally:
        for connection in (setup, down, concurrent):
            if connection is not None:
                connection.close()
        with admin.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")
        admin.close()


def test_down_rejects_durable_session_state_before_column_drop():
    database = f"phase10_down_{uuid4().hex[:16]}"
    admin = _connect()
    db = None
    try:
        with admin.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4")
        db = _connect(database)
        _execute_script(db, BASE_SCHEMA_SQL)
        _execute_phase10_up(db)
        evidence = _archive_down_evidence(db)
        with db.cursor() as cursor:
            cursor.execute(
                "INSERT INTO wecom_inbound_event "
                "(msg_id, from_userid, msg_type, status, "
                "session_commit_deadline_epoch) "
                "VALUES ('durable-msg', 'durable-user', 'text', "
                "'session_pending', UNIX_TIMESTAMP(NOW(6)) + 1800)"
            )

        _set_down_evidence(db, evidence)
        with pytest.raises(pymysql.err.ProgrammingError) as exc_info:
            _execute_script(db, DOWN_SQL)
        assert "phase10_down_guard_failed_new_model_data_exists" in str(exc_info.value)
        db.rollback()

        with db.cursor() as cursor:
            cursor.execute(
                "SELECT status, session_commit_deadline_epoch "
                "FROM wecom_inbound_event"
            )
            status, deadline = cursor.fetchone()
            assert status == "session_pending"
            assert deadline is not None
            cursor.execute(
                "SELECT COUNT(*) FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() "
                "AND TABLE_NAME='wecom_inbound_event' "
                "AND COLUMN_NAME IN ("
                "'session_commit_deadline_epoch','session_apply_lease_owner')"
            )
            assert int(cursor.fetchone()[0]) == 2
    finally:
        if db is not None:
            db.close()
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

        _execute_phase10_up(db)
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

        report = _collect_down_report(database)
        assert report == {
            "down_verify_database_account_mismatch": 0,
            "down_verify_global_select_privilege_missing": 0,
            "down_verify_trigger_privilege_missing": 0,
            "old_schema_required_tables_missing": 0,
            "phase10_job_columns_remaining": 0,
            "phase10_session_columns_remaining": 0,
            "old_job_table_contract_mismatch": 0,
            "old_inbound_table_contract_mismatch": 0,
            "old_inbound_constraints_mismatch": 0,
            "old_inbound_referencing_foreign_keys_mismatch": 0,
            "old_inbound_triggers_remaining": 0,
            "old_inbound_column_contract_mismatch": 0,
            "old_inbound_index_contract_mismatch": 0,
            "old_job_column_contract_mismatch": 0,
            "old_job_index_contract_mismatch": 0,
            "old_job_constraints_mismatch": 0,
            "old_job_triggers_remaining": 0,
            "phase10_tables_remaining": 0,
            "phase10_fences_remaining": 0,
            "backup_expected_columns_remaining": 0,
            "backup_job_id_key_contract_mismatch": 0,
            "restored_job_backup_mismatch": 0,
            "backup_duplicate_job_id_rows": 0,
            "restored_job_backup_row_count_mismatch": 0,
            "ready": True,
        }

        with db.cursor() as cursor:
            cursor.execute(
                "CREATE TABLE wecom_outbound_outbox ("
                "id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY, "
                "inbound_event_id BIGINT UNSIGNED NOT NULL, "
                "CONSTRAINT fk_outbox_inbound FOREIGN KEY (inbound_event_id) "
                "REFERENCES wecom_inbound_event(id) ON DELETE RESTRICT"
                ") ENGINE=InnoDB"
            )
            cursor.execute(
                "INSERT INTO wecom_inbound_event "
                "(msg_id, from_userid, msg_type) "
                "VALUES ('reverse-fk-msg', 'reverse-fk-user', 'text')"
            )
            reverse_fk_inbound_id = int(cursor.lastrowid)
            cursor.execute(
                "INSERT INTO wecom_outbound_outbox (inbound_event_id) VALUES (%s)",
                (reverse_fk_inbound_id,),
            )
        report = _collect_down_report(database)
        assert report["old_inbound_referencing_foreign_keys_mismatch"] == 1
        assert report["ready"] is False
        with db.cursor() as cursor:
            with pytest.raises(pymysql.err.IntegrityError) as reverse_fk_error:
                cursor.execute(
                    "DELETE FROM wecom_inbound_event WHERE id=%s",
                    (reverse_fk_inbound_id,),
                )
            assert reverse_fk_error.value.args[0] == 1451
            cursor.execute("DROP TABLE wecom_outbound_outbox")
            cursor.execute(
                "DELETE FROM wecom_inbound_event WHERE id=%s",
                (reverse_fk_inbound_id,),
            )

        with db.cursor() as cursor:
            cursor.execute(
                "CREATE TRIGGER reject_stage_a_inbound "
                "BEFORE INSERT ON wecom_inbound_event FOR EACH ROW "
                "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='blocked by trigger'"
            )
        report = _collect_down_report(database)
        assert report["old_inbound_triggers_remaining"] == 1
        assert report["ready"] is False
        with db.cursor() as cursor:
            with pytest.raises(pymysql.err.OperationalError) as trigger_error:
                cursor.execute(
                    "INSERT INTO wecom_inbound_event "
                    "(msg_id, from_userid, msg_type) "
                    "VALUES ('trigger-msg', 'trigger-user', 'text')"
                )
            assert trigger_error.value.args[0] == 1644
            cursor.execute("DROP TRIGGER reject_stage_a_inbound")
            cursor.execute(
                "ALTER TABLE wecom_inbound_event ADD CONSTRAINT chk_block_text "
                "CHECK (msg_type <> 'text')"
            )
        report = _collect_down_report(database)
        assert report["old_inbound_constraints_mismatch"] == 1
        assert report["ready"] is False
        with db.cursor() as cursor:
            with pytest.raises(pymysql.err.OperationalError) as check_error:
                cursor.execute(
                    "INSERT INTO wecom_inbound_event "
                    "(msg_id, from_userid, msg_type) "
                    "VALUES ('check-msg', 'check-user', 'text')"
                )
            assert check_error.value.args[0] == 3819
            cursor.execute(
                "ALTER TABLE wecom_inbound_event DROP CHECK chk_block_text, "
                "ADD CONSTRAINT fk_inbound_config FOREIGN KEY (from_userid) "
                "REFERENCES system_config(config_key)"
            )
        report = _collect_down_report(database)
        assert report["old_inbound_constraints_mismatch"] == 1
        assert report["ready"] is False
        with db.cursor() as cursor:
            with pytest.raises(pymysql.err.IntegrityError) as foreign_key_error:
                cursor.execute(
                    "INSERT INTO wecom_inbound_event "
                    "(msg_id, from_userid, msg_type) "
                    "VALUES ('fk-msg', 'missing-user', 'text')"
                )
            assert foreign_key_error.value.args[0] == 1452
            cursor.execute(
                "ALTER TABLE wecom_inbound_event "
                "DROP FOREIGN KEY fk_inbound_config"
            )
            cursor.execute(
                "ALTER TABLE wecom_inbound_event "
                "ADD INDEX idx_extra_media_id (media_id)"
            )
        report = _collect_down_report(database)
        assert report["old_inbound_index_contract_mismatch"] == 0
        assert report["ready"] is True

        with db.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE wecom_inbound_event "
                "ADD UNIQUE INDEX uk_extra_from_userid (from_userid)"
            )
        report = _collect_down_report(database)
        assert report["old_inbound_index_contract_mismatch"] == 1
        assert report["ready"] is False

        with db.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE wecom_inbound_event "
                "DROP INDEX uk_extra_from_userid, "
                "DROP INDEX idx_extra_media_id, "
                "ALTER INDEX idx_status_time INVISIBLE"
            )
        report = _collect_down_report(database)
        assert report["old_inbound_index_contract_mismatch"] == 1
        assert report["ready"] is False

        with db.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE wecom_inbound_event "
                "ALTER INDEX idx_status_time VISIBLE"
            )
            cursor.execute(
                "CREATE INDEX idx_content_numeric ON wecom_inbound_event "
                "((CAST(content_brief AS UNSIGNED)))"
            )
        report = _collect_down_report(database)
        assert report["old_inbound_index_contract_mismatch"] == 1
        assert report["ready"] is False
        with db.cursor() as cursor:
            with pytest.raises(pymysql.MySQLError) as expression_error:
                cursor.execute(
                    "INSERT INTO wecom_inbound_event "
                    "(msg_id, from_userid, msg_type, content_brief) "
                    "VALUES ('function-msg', 'function-user', 'text', '中文')"
                )
            assert expression_error.value.args[0] == 3751
            cursor.execute(
                "ALTER TABLE wecom_inbound_event "
                "DROP INDEX idx_content_numeric"
            )
            cursor.execute("ALTER TABLE wecom_inbound_event ENGINE=MyISAM")
        report = _collect_down_report(database)
        assert report["old_inbound_table_contract_mismatch"] == 1
        assert report["old_inbound_column_contract_mismatch"] == 0
        assert report["ready"] is False

        with db.cursor() as cursor:
            cursor.execute("ALTER TABLE wecom_inbound_event ENGINE=InnoDB")
            cursor.execute(
                "ALTER TABLE wecom_inbound_event "
                "DEFAULT CHARACTER SET latin1 COLLATE latin1_swedish_ci"
            )
        report = _collect_down_report(database)
        assert report["old_inbound_table_contract_mismatch"] == 1
        assert report["old_inbound_column_contract_mismatch"] == 0
        assert report["ready"] is False

        with db.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE wecom_inbound_event "
                "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
            )
            cursor.execute(
                "ALTER TABLE wecom_inbound_event "
                "ADD required_extra VARCHAR(8) NOT NULL"
            )
        report = _collect_down_report(database)
        assert report["old_inbound_column_contract_mismatch"] == 1
        assert report["ready"] is False

        with db.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE wecom_inbound_event DROP COLUMN required_extra, "
                "MODIFY msg_type "
                "ENUM('text','IMAGE','voice','video','file','link','location',"
                "'event','other') NOT NULL"
            )
        report = _collect_down_report(database)
        assert report["old_inbound_column_contract_mismatch"] == 1
        assert report["ready"] is False

        with db.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE wecom_inbound_event MODIFY msg_type "
                "ENUM('text','image','voice','video','file','link','location',"
                "'event','other') NOT NULL, "
                "MODIFY content_brief VARCHAR(500) "
                "CHARACTER SET latin1 COLLATE latin1_swedish_ci NULL"
            )
        report = _collect_down_report(database)
        assert report["old_inbound_column_contract_mismatch"] == 1
        assert report["ready"] is False

        with db.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE wecom_inbound_event "
                "MODIFY content_brief VARCHAR(500) "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NULL, "
                "MODIFY created_at DATETIME(6) NOT NULL "
                "DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"
            )
        report = _collect_down_report(database)
        assert report["old_inbound_column_contract_mismatch"] == 1
        assert report["ready"] is False

        with db.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE wecom_inbound_event "
                "MODIFY created_at DATETIME(6) NOT NULL "
                "DEFAULT CURRENT_TIMESTAMP(6), "
                "MODIFY content_brief VARCHAR(500) "
                "GENERATED ALWAYS AS (LEFT(msg_id, 500)) STORED"
            )
        report = _collect_down_report(database)
        assert report["old_inbound_column_contract_mismatch"] == 1
        assert report["ready"] is False

        with db.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE wecom_inbound_event "
                "MODIFY content_brief VARCHAR(500) NULL"
            )
            cursor.execute(
                "ALTER TABLE wecom_inbound_event "
                "MODIFY retry_count INT NULL DEFAULT 7"
            )
        report = _collect_down_report(database)
        assert report["old_inbound_column_contract_mismatch"] == 1
        assert report["ready"] is False

        with db.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE wecom_inbound_event "
                "MODIFY retry_count TINYINT UNSIGNED NOT NULL DEFAULT 0"
            )
            cursor.execute(
                "ALTER TABLE wecom_inbound_event DROP INDEX idx_session_commit_due"
            )
        report = _collect_down_report(database)
        assert report["old_inbound_column_contract_mismatch"] == 0
        assert report["old_inbound_index_contract_mismatch"] == 1
        assert report["ready"] is False

        with db.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE wecom_inbound_event ADD INDEX idx_session_commit_due "
                "(status, session_next_attempt_at, session_apply_locked_at, id)"
            )
            cursor.execute(
                "ALTER TABLE wecom_inbound_event DROP INDEX uk_msg_id, "
                "ADD UNIQUE INDEX uk_msg_id (msg_id(8))"
            )
        report = _collect_down_report(database)
        assert report["old_inbound_column_contract_mismatch"] == 0
        assert report["old_inbound_index_contract_mismatch"] == 1
        assert report["ready"] is False

        with db.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE wecom_inbound_event DROP INDEX uk_msg_id, "
                "ADD UNIQUE INDEX uk_msg_id (msg_id)"
            )
            cursor.execute("DROP TABLE wecom_inbound_event")
        report = _collect_down_report(database)
        assert report["old_schema_required_tables_missing"] == 1
        assert report["ready"] is False

        with db.cursor() as cursor:
            cursor.execute(
                "CREATE TABLE wecom_inbound_event (id BIGINT PRIMARY KEY)"
            )
        report = _collect_down_report(database)
        assert report["old_schema_required_tables_missing"] == 0
        assert report["old_inbound_column_contract_mismatch"] > 0
        assert report["old_inbound_index_contract_mismatch"] > 0
        assert report["ready"] is False

        with db.cursor() as cursor:
            cursor.execute("DROP TABLE wecom_inbound_event")
            cursor.execute("CREATE VIEW wecom_inbound_event AS SELECT 1 AS id")
        report = _collect_down_report(database)
        assert report["old_schema_required_tables_missing"] == 1
        assert report["ready"] is False

        with db.cursor() as cursor:
            cursor.execute("DROP VIEW wecom_inbound_event")
            cursor.execute("SELECT @@lower_case_table_names")
            assert int(cursor.fetchone()[0]) == 0
            cursor.execute("CREATE TABLE Wecom_Inbound_Event (id BIGINT PRIMARY KEY)")
        report = _collect_down_report(database)
        assert report["old_schema_required_tables_missing"] == 1
        assert report["ready"] is False
    finally:
        if db is not None:
            db.close()
        with admin.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")
        admin.close()
