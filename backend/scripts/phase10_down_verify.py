"""Verify the old-schema contract after an approved destructive Phase 10 down."""
from __future__ import annotations

import json

from sqlalchemy import text

from app.db import SessionLocal


INBOUND_COLUMN_CONTRACT = (
    # name, type, nullable, default, charset, collation, extra, generation
    ("id", "bigint unsigned", "NO", None, None, None, "auto_increment", ""),
    (
        "msg_id", "varchar(64)", "NO", None,
        "utf8mb4", "utf8mb4_0900_ai_ci", "", "",
    ),
    (
        "from_userid", "varchar(64)", "NO", None,
        "utf8mb4", "utf8mb4_0900_ai_ci", "", "",
    ),
    (
        "msg_type",
        "enum('text','image','voice','video','file','link','location','event','other')",
        "NO",
        None,
        "utf8mb4",
        "utf8mb4_0900_ai_ci",
        "",
        "",
    ),
    (
        "media_id", "varchar(128)", "YES", None,
        "utf8mb4", "utf8mb4_0900_ai_ci", "", "",
    ),
    (
        "content_brief", "varchar(500)", "YES", None,
        "utf8mb4", "utf8mb4_0900_ai_ci", "", "",
    ),
    (
        "status",
        "enum('received','processing','session_pending','done','failed','dead_letter')",
        "NO",
        "received",
        "utf8mb4",
        "utf8mb4_0900_ai_ci",
        "",
        "",
    ),
    ("retry_count", "tinyint unsigned", "NO", "0", None, None, "", ""),
    (
        "session_operation", "varchar(8)", "YES", None,
        "utf8mb4", "utf8mb4_0900_ai_ci", "", "",
    ),
    (
        "session_expected_version", "int unsigned", "YES", None,
        None, None, "", "",
    ),
    ("session_payload", "json", "YES", None, None, None, "", ""),
    (
        "session_apply_attempts", "int unsigned", "NO", "0",
        None, None, "", "",
    ),
    (
        "session_apply_locked_at", "datetime(6)", "YES", None,
        None, None, "", "",
    ),
    (
        "session_next_attempt_at", "datetime(6)", "YES", None,
        None, None, "", "",
    ),
    (
        "session_applied_at", "datetime(6)", "YES", None,
        None, None, "", "",
    ),
    (
        "worker_started_at", "datetime(6)", "YES", None,
        None, None, "", "",
    ),
    (
        "worker_finished_at", "datetime(6)", "YES", None,
        None, None, "", "",
    ),
    (
        "error_message", "text", "YES", None,
        "utf8mb4", "utf8mb4_0900_ai_ci", "", "",
    ),
    (
        "created_at", "datetime(6)", "NO", "CURRENT_TIMESTAMP(6)",
        None, None, "DEFAULT_GENERATED", "",
    ),
)

INBOUND_INDEX_CONTRACT = (
    # name, non_unique, ordered columns
    ("PRIMARY", 0, "id"),
    ("uk_msg_id", 0, "msg_id"),
    ("idx_status_time", 1, "status,created_at"),
    ("idx_status_worker_started", 1, "status,worker_started_at"),
    ("idx_status_worker_finished", 1, "status,worker_finished_at"),
    ("idx_from_user", 1, "from_userid,created_at"),
    ("idx_user_status_id", 1, "from_userid,status,id"),
    (
        "idx_session_commit_due",
        1,
        "status,session_next_attempt_at,session_apply_locked_at,id",
    ),
)


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sql_nullable_string(value: str | None) -> str:
    return "NULL" if value is None else _sql_string(value)


def _column_contract_sql() -> str:
    rows = []
    for (
        name,
        column_type,
        nullable,
        default,
        charset,
        collation,
        extra,
        generation,
    ) in INBOUND_COLUMN_CONTRACT:
        rows.append(
            "SELECT "
            f"{_sql_string(name)} AS column_name, "
            f"{_sql_string(column_type)} AS column_type, "
            f"{_sql_string(nullable)} AS is_nullable, "
            f"{_sql_nullable_string(default)} AS column_default, "
            f"{_sql_nullable_string(charset)} AS character_set_name, "
            f"{_sql_nullable_string(collation)} AS collation_name, "
            f"{_sql_string(extra)} AS extra, "
            f"{_sql_string(generation)} AS generation_expression"
        )
    expected = " UNION ALL ".join(rows)
    expected_names = ",".join(
        _sql_string(row[0]) for row in INBOUND_COLUMN_CONTRACT
    )
    return (
        "SELECT (SELECT COUNT(*) FROM (" + expected + ") expected "
        "LEFT JOIN information_schema.COLUMNS actual "
        "ON actual.TABLE_SCHEMA=DATABASE() "
        "AND BINARY actual.TABLE_NAME='wecom_inbound_event' "
        "AND BINARY actual.COLUMN_NAME=expected.column_name "
        "WHERE actual.COLUMN_NAME IS NULL "
        "OR NOT (BINARY actual.COLUMN_TYPE <=> BINARY expected.column_type) "
        "OR NOT (BINARY actual.IS_NULLABLE <=> BINARY expected.is_nullable) "
        "OR NOT (BINARY actual.COLUMN_DEFAULT <=> BINARY expected.column_default) "
        "OR NOT (BINARY actual.CHARACTER_SET_NAME "
        "<=> BINARY expected.character_set_name) "
        "OR NOT (BINARY actual.COLLATION_NAME <=> BINARY expected.collation_name) "
        "OR NOT (BINARY actual.EXTRA <=> BINARY expected.extra) "
        "OR NOT (BINARY actual.GENERATION_EXPRESSION "
        "<=> BINARY expected.generation_expression)) + "
        "(SELECT COUNT(*) FROM information_schema.COLUMNS actual "
        "WHERE actual.TABLE_SCHEMA=DATABASE() "
        "AND BINARY actual.TABLE_NAME='wecom_inbound_event' "
        f"AND BINARY actual.COLUMN_NAME NOT IN ({expected_names}))"
    )


def _index_contract_sql() -> str:
    rows = []
    for name, non_unique, columns in INBOUND_INDEX_CONTRACT:
        rows.append(
            "SELECT "
            f"{_sql_string(name)} AS index_name, "
            f"{non_unique} AS non_unique, "
            f"{_sql_string(columns)} AS columns"
        )
    expected = " UNION ALL ".join(rows)
    return (
        "SELECT COUNT(*) FROM (" + expected + ") expected "
        "LEFT JOIN ("
        "SELECT INDEX_NAME, MIN(NON_UNIQUE) AS non_unique, "
        "GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX SEPARATOR ',') AS columns, "
        "SUM(CASE WHEN SUB_PART IS NOT NULL OR EXPRESSION IS NOT NULL "
        "THEN 1 ELSE 0 END) AS partial_or_expression_columns "
        "FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA=DATABASE() "
        "AND BINARY TABLE_NAME='wecom_inbound_event' GROUP BY INDEX_NAME"
        ") actual ON BINARY actual.INDEX_NAME=expected.index_name "
        "WHERE actual.INDEX_NAME IS NULL "
        "OR actual.non_unique<>expected.non_unique "
        "OR actual.columns<>expected.columns "
        "OR actual.partial_or_expression_columns<>0"
    )


SCHEMA_CHECKS = {
    "old_schema_required_tables_missing": (
        "SELECT 3 - COUNT(*) FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_TYPE='BASE TABLE' "
        "AND BINARY TABLE_NAME IN ("
        "'job','phase10_job_lifecycle_backup','wecom_inbound_event')"
    ),
    "phase10_job_columns_remaining": (
        "SELECT COUNT(*) FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='job' "
        "AND COLUMN_NAME IN ('activated_at','candidate_expires_at')"
    ),
    "phase10_session_columns_remaining": (
        "SELECT COUNT(*) FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='wecom_inbound_event' "
        "AND COLUMN_NAME IN ("
        "'session_commit_deadline_epoch','session_apply_lease_owner')"
    ),
    "old_inbound_table_contract_mismatch": (
        "SELECT CASE WHEN COUNT(*)=1 "
        "AND MAX(BINARY tables.ENGINE='InnoDB')=1 "
        "AND MAX(BINARY charsets.CHARACTER_SET_NAME='utf8mb4')=1 "
        "AND MAX(BINARY tables.TABLE_COLLATION='utf8mb4_0900_ai_ci')=1 "
        "THEN 0 ELSE 1 END "
        "FROM information_schema.TABLES tables "
        "LEFT JOIN information_schema.COLLATION_CHARACTER_SET_APPLICABILITY charsets "
        "ON BINARY charsets.COLLATION_NAME=tables.TABLE_COLLATION "
        "WHERE tables.TABLE_SCHEMA=DATABASE() "
        "AND tables.TABLE_TYPE='BASE TABLE' "
        "AND BINARY tables.TABLE_NAME='wecom_inbound_event'"
    ),
    "old_inbound_column_contract_mismatch": _column_contract_sql(),
    "old_inbound_index_contract_mismatch": _index_contract_sql(),
    "old_job_column_contract_mismatch": (
        "SELECT CASE WHEN "
        "(SELECT COUNT(*) FROM information_schema.COLUMNS "
        " WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='job' "
        " AND COLUMN_NAME='expires_at' AND IS_NULLABLE='NO') = 1 "
        "AND (SELECT COUNT(*) FROM information_schema.COLUMNS "
        " WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='job' "
        " AND COLUMN_NAME='delist_reason' "
        " AND COLUMN_TYPE=\"enum('filled','manual_delist','expired')\") = 1 "
        "THEN 0 ELSE 1 END"
    ),
    "phase10_tables_remaining": (
        "SELECT COUNT(*) FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN ("
        "'job_replacement','media_asset_lifecycle','target_cleanup_task',"
        "'phase10_migration_control')"
    ),
    "phase10_fences_remaining": (
        "SELECT "
        "(SELECT COUNT(*) FROM information_schema.TRIGGERS "
        " WHERE TRIGGER_SCHEMA=DATABASE() "
        " AND TRIGGER_NAME LIKE 'phase10\\_%\\_fence') + "
        "(SELECT COUNT(*) FROM information_schema.ROUTINES "
        " WHERE ROUTINE_SCHEMA=DATABASE() "
        " AND ROUTINE_NAME='phase10_assert_writes_allowed')"
    ),
    "backup_expected_columns_remaining": (
        "SELECT COUNT(*) FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=DATABASE() "
        "AND TABLE_NAME='phase10_job_lifecycle_backup' "
        "AND COLUMN_NAME LIKE 'expected\\_%'"
    ),
}

RESTORE_MISMATCH_SQL = """
SELECT
  (SELECT COUNT(*)
   FROM phase10_job_lifecycle_backup b
   LEFT JOIN job j ON j.id=b.job_id
   WHERE j.id IS NULL
      OR NOT (j.audit_status <=> b.audit_status)
      OR NOT (j.expires_at <=> b.expires_at)
      OR NOT (j.deleted_at <=> b.deleted_at)
      OR NOT (j.delist_reason <=> b.delist_reason)
      OR NOT (j.version <=> b.version)
      OR NOT (j.updated_at <=> b.source_updated_at))
  +
  (SELECT COUNT(*)
   FROM job j
   LEFT JOIN phase10_job_lifecycle_backup b ON b.job_id=j.id
   WHERE b.job_id IS NULL)
"""


def collect(db) -> dict[str, int | bool]:
    report = {
        name: int(db.execute(text(sql)).scalar() or 0)
        for name, sql in SCHEMA_CHECKS.items()
    }
    required_tables = int(db.execute(text(
        "SELECT COUNT(*) FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA=DATABASE() "
        "AND TABLE_NAME IN ('job','phase10_job_lifecycle_backup')"
    )).scalar() or 0)
    if required_tables == 2:
        report["restored_job_backup_mismatch"] = int(
            db.execute(text(RESTORE_MISMATCH_SQL)).scalar() or 0
        )
    else:
        report["restored_job_backup_mismatch"] = 1
    report["ready"] = all(value == 0 for value in report.values())
    return report


def main() -> int:
    with SessionLocal() as db:
        report = collect(db)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
