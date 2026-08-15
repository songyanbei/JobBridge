"""Verify the old-schema contract after an approved destructive Phase 10 down."""
from __future__ import annotations

import json

from sqlalchemy import text

from app.db import SessionLocal


SCHEMA_CHECKS = {
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
