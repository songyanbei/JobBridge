"""Read-only deployment gates for the Phase 10 Job lifecycle rollout."""
from __future__ import annotations

import json

from redis.exceptions import RedisError
from sqlalchemy import text

from app.core.redis_client import (
    RedisDurabilityPolicyError,
    validate_redis_durability_policy,
)
from app.config import settings
from app.db import SessionLocal
from scripts.backfill_media_lifecycle import backfill_media_lifecycle


CHECKS = {
    "session_commit_deadline_schema_mismatch": (
        "SELECT CASE WHEN EXISTS (SELECT 1 FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='wecom_inbound_event' "
        "AND COLUMN_NAME='session_commit_deadline_epoch' "
        "AND DATA_TYPE='decimal' AND COLUMN_TYPE='decimal(20,6)' "
        "AND NUMERIC_PRECISION=20 AND NUMERIC_SCALE=6 "
        "AND IS_NULLABLE='YES') THEN 0 ELSE 1 END"
    ),
    "session_apply_lease_owner_schema_mismatch": (
        "SELECT CASE WHEN EXISTS (SELECT 1 FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='wecom_inbound_event' "
        "AND COLUMN_NAME='session_apply_lease_owner' "
        "AND DATA_TYPE='varchar' AND COLUMN_TYPE='varchar(64)' "
        "AND CHARACTER_MAXIMUM_LENGTH=64 AND IS_NULLABLE='YES') "
        "THEN 0 ELSE 1 END"
    ),
    "session_commit_due_index_mismatch": (
        "SELECT CASE WHEN COALESCE((SELECT GROUP_CONCAT(COLUMN_NAME "
        "ORDER BY SEQ_IN_INDEX SEPARATOR ',') FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='wecom_inbound_event' "
        "AND INDEX_NAME='idx_session_commit_due'), '')="
        "'status,session_next_attempt_at,session_apply_locked_at,id' "
        "AND COALESCE((SELECT MIN(NON_UNIQUE) FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='wecom_inbound_event' "
        "AND INDEX_NAME='idx_session_commit_due'), -1)=1 THEN 0 ELSE 1 END"
    ),
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
    "invalid_hard_delete_delay_config": (
        "SELECT CASE WHEN EXISTS (SELECT 1 FROM system_config "
        "WHERE config_key='ttl.hard_delete.delay_days' "
        "AND config_value REGEXP '^[0-9]+$' "
        "AND CAST(config_value AS UNSIGNED) BETWEEN 0 AND 3650) "
        "THEN 0 ELSE 1 END"
    ),
    "job_backup_coverage_mismatch": (
        "SELECT CASE WHEN EXISTS (SELECT 1 FROM phase10_migration_control c "
        "WHERE c.id=1 "
        "AND c.backup_rows=(SELECT COUNT(*) FROM phase10_job_lifecycle_backup) "
        "AND c.backup_checksum=(SELECT COALESCE(BIT_XOR(CRC32(CONCAT_WS('|', "
        "job_id, audit_status, COALESCE(expires_at, ''), COALESCE(deleted_at, ''), "
        "COALESCE(delist_reason, ''), version))), 0) "
        "FROM phase10_job_lifecycle_backup) "
        "AND c.source_soft_deleted_rows=(SELECT COUNT(*) "
        "FROM phase10_job_lifecycle_backup WHERE deleted_at IS NOT NULL) "
        "AND c.source_passed_online_rows=(SELECT COUNT(*) "
        "FROM phase10_job_lifecycle_backup WHERE deleted_at IS NULL "
        "AND audit_status='passed') "
        "AND c.source_candidate_rows=(SELECT COUNT(*) "
        "FROM phase10_job_lifecycle_backup WHERE deleted_at IS NULL "
        "AND audit_status IN ('pending','rejected')) "
        "AND c.backup_rows=c.source_soft_deleted_rows+"
        "c.source_passed_online_rows+c.source_candidate_rows "
        "AND c.expected_live_checksum=(SELECT COALESCE(BIT_XOR(CRC32(CONCAT_WS('|', "
        "job_id, expected_audit_status, COALESCE(expected_expires_at, ''), "
        "COALESCE(expected_deleted_at, ''), COALESCE(expected_delist_reason, ''), "
        "expected_version, COALESCE(expected_activated_at, ''), "
        "COALESCE(expected_candidate_expires_at, '')))), 0) "
        "FROM phase10_job_lifecycle_backup)) THEN 0 ELSE 1 END"
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
    "media_delete_dead_letter_key_count",
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
    "media_delete_dead_letter_key_count",
    "invalid_images_json_count",
    "unresolved_media_reference_count",
    "media_reference_alias_count",
    "media_reference_conflict_count",
)


def collect_media_coverage(db) -> dict[str, int]:
    report = backfill_media_lifecycle(db, apply=False)
    return {name: int(report[name]) for name in MEDIA_REPORT_FIELDS}


def collect_redis_policy() -> dict[str, int | str]:
    try:
        actual = validate_redis_durability_policy()
    except (RedisDurabilityPolicyError, RedisError) as exc:
        return {
            "redis_durability_policy_mismatch": 1,
            "redis_policy_error": type(exc).__name__,
        }
    return {
        "redis_durability_policy_mismatch": 0,
        "redis_maxmemory_policy": actual["maxmemory-policy"],
        "redis_appendonly": actual["appendonly"],
        "redis_appendfsync": actual["appendfsync"],
    }


def collect_recommendation_content_key_gate(config=settings) -> dict[str, int]:
    """Block replacement rollout when its active encryption key is unavailable."""
    active_version = int(config.recommendation_content_key_active_version)
    return {
        "recommendation_content_key_unavailable": int(
            bool(config.job_replacement_enabled)
            and (
                not 1 <= active_version <= 65_535
                or not config.recommendation_content_key_configured
            )
        ),
        "recommendation_content_key_active_version": active_version,
    }


def collect(db) -> dict:
    counts = {
        name: int(db.execute(text(sql)).scalar() or 0)
        for name, sql in CHECKS.items()
    }
    counts.update(collect_media_coverage(db))
    counts.update(collect_redis_policy())
    counts.update(collect_recommendation_content_key_gate())
    next_id, max_id = db.execute(text(
        "SELECT t.AUTO_INCREMENT, COALESCE(MAX(j.id),0) "
        "FROM information_schema.TABLES t LEFT JOIN job j ON 1=1 "
        "WHERE t.TABLE_SCHEMA=DATABASE() AND t.TABLE_NAME='job' "
        "GROUP BY t.AUTO_INCREMENT"
    )).one()
    counts["job_auto_increment_not_above_max"] = int(not next_id or int(next_id) <= int(max_id))
    blockers = (
        *CHECKS,
        *MEDIA_BLOCKING_CHECKS,
        "redis_durability_policy_mismatch",
        "recommendation_content_key_unavailable",
        "job_auto_increment_not_above_max",
    )
    counts["ready"] = not any(counts[name] for name in blockers)
    return counts


def main() -> int:
    with SessionLocal() as db:
        result = collect(db)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
