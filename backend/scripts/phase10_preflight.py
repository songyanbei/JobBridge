"""Read-only deployment gates for the Phase 10 Job lifecycle rollout."""
from __future__ import annotations

import json

from sqlalchemy import text

from app.db import SessionLocal
from scripts.backfill_media_lifecycle import backfill_media_lifecycle


CHECKS = {
    "media_state_enum_missing_dead_letter": (
        "SELECT CASE WHEN EXISTS (SELECT 1 FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='media_asset_lifecycle' "
        "AND COLUMN_NAME='state' "
        "AND COLUMN_TYPE LIKE '%''dead_letter''%') THEN 0 ELSE 1 END"
    ),
    "invalid_job_ttl_config": (
        "SELECT CASE WHEN EXISTS (SELECT 1 FROM system_config "
        "WHERE config_key='ttl.job.days' AND config_value REGEXP '^[0-9]+$' "
        "AND CAST(config_value AS UNSIGNED) BETWEEN 1 AND 3650) THEN 0 ELSE 1 END"
    ),
    "invalid_candidate_ttl_config": (
        "SELECT CASE WHEN EXISTS (SELECT 1 FROM system_config "
        "WHERE config_key='ttl.job.candidate.days' AND config_value REGEXP '^[0-9]+$' "
        "AND CAST(config_value AS UNSIGNED) BETWEEN 1 AND 365) THEN 0 ELSE 1 END"
    ),
    "job_backup_coverage_mismatch": (
        "SELECT COUNT(*) FROM ("
        "SELECT j.id FROM job j LEFT JOIN phase10_job_lifecycle_backup b ON b.job_id=j.id "
        "WHERE b.job_id IS NULL UNION ALL "
        "SELECT b.job_id FROM phase10_job_lifecycle_backup b LEFT JOIN job j ON j.id=b.job_id "
        "WHERE j.id IS NULL) missing_backup"
    ),
    "passed_without_activation": (
        "SELECT COUNT(*) FROM job WHERE deleted_at IS NULL AND audit_status='passed' "
        "AND (activated_at IS NULL OR expires_at IS NULL)"
    ),
    "invalid_unactivated_candidate": (
        "SELECT COUNT(*) FROM job WHERE deleted_at IS NULL "
        "AND audit_status IN ('pending','rejected') "
        "AND (activated_at IS NOT NULL OR expires_at IS NOT NULL "
        "OR candidate_expires_at IS NULL)"
    ),
    "active_replacement_graph_missing": (
        "SELECT COUNT(*) FROM job_replacement r "
        "LEFT JOIN job old_job ON old_job.id=r.old_job_id "
        "LEFT JOIN job new_job ON new_job.id=r.new_job_id "
        "WHERE r.lifecycle_status IN ('awaiting_review','conflict') "
        "AND (old_job.id IS NULL OR new_job.id IS NULL OR r.active_old_job_id IS NULL)"
    ),
    "soft_deleted_without_cleanup_task": (
        "SELECT COUNT(*) FROM job j LEFT JOIN target_cleanup_task t "
        "ON t.target_type='job' AND t.target_id=j.id "
        "WHERE j.deleted_at IS NOT NULL AND t.id IS NULL"
    ),
}

MEDIA_BLOCKING_CHECKS = (
    "missing_media_lifecycle_key_count",
    "repair_required_media_lifecycle_key_count",
    "non_deleted_soft_deleted_media_key_count",
    "invalid_images_json_count",
    "unresolved_media_reference_count",
    "media_reference_conflict_count",
)

MEDIA_REPORT_FIELDS = (
    "raw_reference_count",
    "normalized_reference_count",
    "normalized_job_image_key_count",
    "normalized_resume_image_key_count",
    "matched_media_lifecycle_key_count",
    "missing_media_lifecycle_key_count",
    "repair_required_media_lifecycle_key_count",
    "non_deleted_soft_deleted_media_key_count",
    "invalid_images_json_count",
    "unresolved_media_reference_count",
    "media_reference_alias_count",
    "media_reference_conflict_count",
)


def collect_media_coverage(db) -> dict[str, int]:
    report = backfill_media_lifecycle(db, apply=False)
    return {name: int(report[name]) for name in MEDIA_REPORT_FIELDS}


def collect(db) -> dict:
    counts = {
        name: int(db.execute(text(sql)).scalar() or 0)
        for name, sql in CHECKS.items()
    }
    counts.update(collect_media_coverage(db))
    next_id, max_id = db.execute(text(
        "SELECT t.AUTO_INCREMENT, COALESCE(MAX(j.id),0) "
        "FROM information_schema.TABLES t LEFT JOIN job j ON 1=1 "
        "WHERE t.TABLE_SCHEMA=DATABASE() AND t.TABLE_NAME='job' "
        "GROUP BY t.AUTO_INCREMENT"
    )).one()
    counts["job_auto_increment_not_above_max"] = int(not next_id or int(next_id) <= int(max_id))
    blockers = (*CHECKS, *MEDIA_BLOCKING_CHECKS, "job_auto_increment_not_above_max")
    counts["ready"] = not any(counts[name] for name in blockers)
    return counts


def main() -> int:
    with SessionLocal() as db:
        result = collect(db)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
