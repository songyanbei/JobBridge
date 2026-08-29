from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts import phase10_down_verify, phase10_preflight


ROOT = Path(__file__).resolve().parents[2]


def _clean_media_coverage():
    return {name: 0 for name in phase10_preflight.MEDIA_REPORT_FIELDS}


@pytest.fixture(autouse=True)
def _stable_redis_policy(monkeypatch):
    monkeypatch.setattr(
        phase10_preflight,
        "validate_redis_durability_policy",
        lambda: {
            "maxmemory-policy": "noeviction",
            "appendonly": "yes",
            "appendfsync": "always",
        },
    )


def test_additive_migration_contains_rollout_invariants():
    sql = (ROOT / "sql/migrations/phase10_001_job_lifecycle_additive.sql").read_text(
        encoding="utf-8"
    )
    assert "phase10_job_lifecycle_backup" in sql
    assert "AUTO_INCREMENT" in sql and "MAX(j.`id`)" in sql
    assert "backup_checksum" in sql
    assert "audit_status` = 'passed'" in sql
    assert "candidate_expires_at` IS NULL" in sql
    for classification in (
        "source_soft_deleted_rows",
        "source_passed_online_rows",
        "source_candidate_rows",
    ):
        assert classification in sql
    assert "phase10_assert_lifecycle_backfill" in sql
    assert "phase10_lifecycle_backfill_evidence_mismatch" in sql
    assert "live_checksum_valid" in sql
    assert "expected_checksum_valid" in sql
    assert "SET @phase10_migration_time = UTC_TIMESTAMP();" in sql
    assert "SET @phase10_migration_time = NOW();" not in sql
    assert "('ttl.job.candidate.days', '7', 'int'" in sql
    assert "ON DUPLICATE KEY UPDATE `config_key` = VALUES(`config_key`)" in sql


def test_media_dead_letter_schema_and_upgrade_migration_are_complete():
    from app.models import MediaAssetLifecycle

    assert "dead_letter" in MediaAssetLifecycle.__table__.columns["state"].type.enums

    additive = (
        ROOT / "sql/migrations/phase10_001_job_lifecycle_additive.sql"
    ).read_text(encoding="utf-8")
    schema = (ROOT / "sql/schema.sql").read_text(encoding="utf-8")
    upgrade = (
        ROOT / "sql/migrations/phase10_002_media_dead_letter.sql"
    ).read_text(encoding="utf-8")

    for sql in (additive, schema, upgrade):
        assert "'dead_letter'" in sql
    assert "ALTER TABLE `media_asset_lifecycle`" in upgrade
    assert "`attempt_count` >= 10" in upgrade
    assert "`state` = 'dead_letter'" in upgrade
    assert "`next_attempt_at` = NULL" in upgrade
    assert "`lease_owner` = NULL" in upgrade
    assert "`lease_expires_at` = NULL" in upgrade


def test_down_migration_blocks_new_model_data_and_validates_restore_checksum():
    sql = (ROOT / "sql/migrations/phase10_down_001_job_lifecycle.sql").read_text(
        encoding="utf-8"
    )
    for table in ("job_replacement", "target_cleanup_task", "media_asset_lifecycle"):
        assert f"FROM `{table}`" in sql
    assert "FROM `wecom_inbound_event`" in sql
    assert "`status` = 'session_pending'" in sql
    assert "`session_commit_deadline_epoch` IS NOT NULL" in sql
    assert "`session_apply_lease_owner` IS NOT NULL" in sql
    assert "DROP COLUMN `session_apply_lease_owner`" in sql
    assert "DROP COLUMN `session_commit_deadline_epoch`" in sql
    for action in ("insert", "update", "delete"):
        assert f"DROP TRIGGER `phase10_inbound_{action}_fence`" in sql
    assert "phase10_down_blocked" in sql
    assert "COUNT(*) FROM `phase10_job_lifecycle_backup`" in sql
    assert "phase10_restore_checksum_valid" in sql
    assert "phase10_post_ddl_restore_checksum_valid" in sql
    assert "phase10_down_guard_failed_checksum_mismatch" in sql
    assert "START TRANSACTION" in sql
    assert "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ" in sql
    assert "@phase10_destructive_down_authorized = 1" in sql
    for archived_value in (
        "phase10_archived_backup_rows",
        "phase10_archived_backup_checksum",
        "phase10_archived_expected_live_checksum",
    ):
        assert archived_value in sql
    assert "FROM `job` FOR UPDATE" in sql
    assert "FROM `phase10_job_lifecycle_backup` FOR SHARE" in sql
    assert sql.index("COMMIT;") < sql.index("ALTER TABLE `job` DROP INDEX")
    update_offset = sql.index("UPDATE `job` AS j")
    drift_guard_offset = sql.index("expected_candidate_expires_at")
    assert drift_guard_offset < update_offset
    for field in (
        "audit_status",
        "expires_at",
        "deleted_at",
        "delist_reason",
        "version",
        "updated_at",
        "activated_at",
        "candidate_expires_at",
    ):
        assert f"j.`{field}` <=> b.`expected_{field}`" in sql


def test_additive_migration_freezes_post_migration_state_for_safe_down():
    sql = (ROOT / "sql/migrations/phase10_001_job_lifecycle_additive.sql").read_text(
        encoding="utf-8"
    )
    for action in ("INSERT", "UPDATE", "DELETE"):
        assert f"BEFORE {action} ON `wecom_inbound_event`" in sql
    snapshot_offset = sql.index("expected_audit_status")
    final_backfill_offset = sql.index(
        "WHERE `deleted_at` IS NULL AND `audit_status` IN ('pending', 'rejected')"
    )
    assert final_backfill_offset < snapshot_offset
    assert "expected_live_checksum" in sql
    expected_projection = sql[
        sql.index("UPDATE `phase10_job_lifecycle_backup` AS b"):sql.index(
            "ALTER TABLE `phase10_job_lifecycle_backup`\n  MODIFY COLUMN"
        )
    ]
    assert "JOIN `job` AS j ON j.`id` = b.`job_id`" in expected_projection
    assert "b.`expected_version` = b.`version` + 1" in expected_projection
    assert "b.`expected_updated_at` = j.`updated_at`" in expected_projection
    assert "MODIFY COLUMN `source_updated_at` DATETIME NOT NULL" in sql
    for source_field in ("source_created_at", "source_audited_at"):
        assert f"DROP COLUMN `{source_field}`" in expected_projection
    assert "DROP COLUMN `source_updated_at`" not in expected_projection
    assert "phase10_migration_control" in sql
    assert "phase10_assert_writes_allowed" in sql
    for trigger in (
        "phase10_job_insert_fence",
        "phase10_job_update_fence",
        "phase10_job_delete_fence",
        "phase10_replacement_insert_fence",
        "phase10_cleanup_insert_fence",
        "phase10_media_insert_fence",
    ):
        assert trigger in sql
    assert "expected_audit_status` ENUM('pending','passed','rejected') NOT NULL" in sql
    assert "expected_version` INT UNSIGNED NOT NULL" in sql
    assert "expected_updated_at` DATETIME NOT NULL" in sql
    assert "SET j.`updated_at` = b.`source_updated_at`" in (
        ROOT / "sql/migrations/phase10_down_001_job_lifecycle.sql"
    ).read_text(encoding="utf-8")


def test_preflight_reports_all_clean_database_as_ready(monkeypatch):
    monkeypatch.setattr(
        phase10_preflight, "collect_media_coverage", lambda _: _clean_media_coverage()
    )
    db = MagicMock()
    scalar_results = [MagicMock() for _ in phase10_preflight.CHECKS]
    for result in scalar_results:
        result.scalar.return_value = 0
    auto_increment = MagicMock()
    auto_increment.one.return_value = (101, 100)
    db.execute.side_effect = [*scalar_results, auto_increment]

    result = phase10_preflight.collect(db)

    assert result["ready"] is True
    assert result["job_auto_increment_not_above_max"] == 0


def test_preflight_includes_config_and_backup_coverage_gates():
    assert "session_commit_deadline_schema_mismatch" in phase10_preflight.CHECKS
    assert "session_apply_lease_owner_schema_mismatch" in phase10_preflight.CHECKS
    assert "session_commit_due_index_mismatch" in phase10_preflight.CHECKS
    assert "media_state_enum_missing_dead_letter" in phase10_preflight.CHECKS
    assert "invalid_job_ttl_config" in phase10_preflight.CHECKS
    assert "invalid_candidate_ttl_config" in phase10_preflight.CHECKS
    assert "job_backup_coverage_mismatch" in phase10_preflight.CHECKS
    backup_gate = phase10_preflight.CHECKS["job_backup_coverage_mismatch"]
    assert "LEFT JOIN job" not in backup_gate
    assert "phase10_migration_control" in backup_gate
    for field in ("backup_rows", "backup_checksum", "expected_live_checksum"):
        assert field in backup_gate
    for field in (
        "source_soft_deleted_rows",
        "source_passed_online_rows",
        "source_candidate_rows",
    ):
        assert field in backup_gate
    enum_gate = phase10_preflight.CHECKS["media_state_enum_missing_dead_letter"]
    assert "information_schema.COLUMNS" in enum_gate
    assert "COLUMN_TYPE" in enum_gate
    assert "dead_letter" in enum_gate


def test_additive_migration_freezes_backup_integrity_evidence():
    sql = (ROOT / "sql/migrations/phase10_001_job_lifecycle_additive.sql").read_text(
        encoding="utf-8"
    )
    snapshot = sql.index("UPDATE `phase10_job_lifecycle_backup` AS b")
    evidence = sql.index("UPDATE `phase10_migration_control`", snapshot)
    final_gate = sql.index("-- Deployment gate", evidence)

    assert snapshot < evidence < final_gate
    for field in ("backup_rows", "backup_checksum", "expected_live_checksum"):
        assert f"`{field}` BIGINT UNSIGNED NOT NULL" in sql


def test_session_schema_gates_check_exact_types_nullable_and_index_order():
    deadline_gate = phase10_preflight.CHECKS[
        "session_commit_deadline_schema_mismatch"
    ]
    owner_gate = phase10_preflight.CHECKS[
        "session_apply_lease_owner_schema_mismatch"
    ]
    index_gate = phase10_preflight.CHECKS["session_commit_due_index_mismatch"]

    assert "COLUMN_TYPE='decimal(20,6)'" in deadline_gate
    assert "NUMERIC_PRECISION=20" in deadline_gate
    assert "NUMERIC_SCALE=6" in deadline_gate
    assert "IS_NULLABLE='YES'" in deadline_gate
    assert "COLUMN_TYPE='varchar(64)'" in owner_gate
    assert "CHARACTER_MAXIMUM_LENGTH=64" in owner_gate
    assert "IS_NULLABLE='YES'" in owner_gate
    assert "ORDER BY SEQ_IN_INDEX" in index_gate
    assert "status,session_next_attempt_at,session_apply_locked_at,id" in index_gate


def test_preflight_fails_when_live_redis_policy_is_unsafe(monkeypatch):
    monkeypatch.setattr(
        phase10_preflight, "collect_media_coverage", lambda _: _clean_media_coverage()
    )
    monkeypatch.setattr(
        phase10_preflight,
        "collect_redis_policy",
        lambda: {
            "redis_durability_policy_mismatch": 1,
            "redis_policy_error": "RedisDurabilityPolicyError",
        },
    )
    db = MagicMock()
    scalar_results = [MagicMock() for _ in phase10_preflight.CHECKS]
    for result in scalar_results:
        result.scalar.return_value = 0
    auto_increment = MagicMock()
    auto_increment.one.return_value = (101, 100)
    db.execute.side_effect = [*scalar_results, auto_increment]

    result = phase10_preflight.collect(db)

    assert result["ready"] is False
    assert result["redis_durability_policy_mismatch"] == 1


@pytest.mark.parametrize(
    ("config_values", "expected"),
    [
        (
            {
                "job_replacement_enabled": True,
                "recommendation_content_key": "active-secret",
                "recommendation_content_key_active_version": 65_535,
            },
            0,
        ),
        (
            {
                "job_replacement_enabled": True,
                "recommendation_content_key_ring": "1:old-secret,2:active-secret",
                "recommendation_content_key_active_version": 2,
            },
            0,
        ),
        ({"job_replacement_enabled": True}, 1),
        (
            {
                "job_replacement_enabled": True,
                "recommendation_content_key_ring": "1:old-secret",
                "recommendation_content_key_active_version": 2,
            },
            1,
        ),
        ({"job_replacement_enabled": False}, 0),
    ],
)
def test_replacement_content_key_gate(config_values, expected):
    from app.config import Settings

    values = {
        "recommendation_content_key": "",
        "recommendation_content_key_ring": "",
        "recommendation_content_key_active_version": 1,
        **config_values,
    }
    config = Settings(_env_file=None, **values)

    result = phase10_preflight.collect_recommendation_content_key_gate(config)

    assert result["recommendation_content_key_unavailable"] == expected
    assert result["recommendation_content_key_active_version"] == int(
        config.recommendation_content_key_active_version
    )


def test_replacement_content_key_version_above_smallint_range_is_blocked():
    from app.config import Settings

    config = MagicMock(
        job_replacement_enabled=True,
        recommendation_content_key_configured=True,
        recommendation_content_key_active_version=65_536,
    )

    result = phase10_preflight.collect_recommendation_content_key_gate(config)

    assert result["recommendation_content_key_unavailable"] == 1
    assert result["recommendation_content_key_active_version"] == 65_536
    with pytest.raises(ValueError, match="between 1 and 65535"):
        Settings(
            _env_file=None,
            job_replacement_enabled=True,
            recommendation_content_key="active-secret",
            recommendation_content_key_active_version=65_536,
        )


def test_preflight_fails_when_replacement_content_key_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        phase10_preflight, "collect_media_coverage", lambda _: _clean_media_coverage()
    )
    monkeypatch.setattr(
        phase10_preflight,
        "collect_recommendation_content_key_gate",
        lambda: {
            "recommendation_content_key_unavailable": 1,
            "recommendation_content_key_active_version": 2,
        },
    )
    db = MagicMock()
    scalar_results = [MagicMock() for _ in phase10_preflight.CHECKS]
    for result in scalar_results:
        result.scalar.return_value = 0
    auto_increment = MagicMock()
    auto_increment.one.return_value = (101, 100)
    db.execute.side_effect = [*scalar_results, auto_increment]

    result = phase10_preflight.collect(db)

    assert result["ready"] is False
    assert result["recommendation_content_key_unavailable"] == 1
    assert result["recommendation_content_key_active_version"] == 2


@pytest.mark.parametrize(
    "error",
    [
        phase10_preflight.RedisDurabilityPolicyError("unsafe"),
        phase10_preflight.RedisError("unavailable"),
    ],
)
def test_redis_policy_collection_fails_closed(monkeypatch, error):
    monkeypatch.setattr(
        phase10_preflight,
        "validate_redis_durability_policy",
        MagicMock(side_effect=error),
    )

    result = phase10_preflight.collect_redis_policy()

    assert result == {
        "redis_durability_policy_mismatch": 1,
        "redis_policy_error": type(error).__name__,
    }


def test_fresh_schema_contains_phase10_session_columns_in_final_order():
    schema = (ROOT / "sql/schema.sql").read_text(encoding="utf-8")
    locked = schema.index("`session_apply_locked_at` DATETIME(6)")
    owner = schema.index("`session_apply_lease_owner` VARCHAR(64)")
    next_attempt = schema.index("`session_next_attempt_at` DATETIME(6)")
    deadline = schema.index("`session_commit_deadline_epoch` DECIMAL(20,6)")
    applied = schema.index("`session_applied_at` DATETIME(6)")

    assert locked < owner < next_attempt < deadline < applied


def test_release_manual_orders_target_backfill_before_full_preflight():
    manual = (ROOT.parent / "docs/岗位生命周期Phase10发布手册.md").read_text(
        encoding="utf-8"
    )
    dry_run = manual.index("python -m scripts.backfill_target_cleanup_tasks\n")
    apply = manual.index("python -m scripts.backfill_target_cleanup_tasks --apply")
    missing_zero = manual.index("missing=0")
    preflight = manual.index("python -m scripts.phase10_preflight")

    assert dry_run < apply < missing_zero < preflight


def test_release_manual_deploys_old_schema_compatibility_before_migration():
    manual = (ROOT.parent / "docs/岗位生命周期Phase10发布手册.md").read_text(
        encoding="utf-8"
    )
    stage_a_commit = "499eb929b75ad2f208d306b62157d8ded0119f33"
    release_head = manual.index('git -C "$PHASE10_RELEASE_ROOT" rev-parse HEAD')
    release_clean = manual.index('git -C "$PHASE10_RELEASE_ROOT" status --porcelain')
    ancestor = manual.index("merge-base --is-ancestor")
    stage_a_worktree = manual.index("worktree add --detach")
    stage_a_deploy = manual.index("部署为 Stage A 同一镜像 digest")
    stage_a_smoke = manual.index("后台岗位列表第 1/2 页")
    migration = manual.index("phase10_001_job_lifecycle_additive.sql")
    final_deploy = manual.index("构建一次最终 schema-aware 制品")

    assert stage_a_commit in manual
    assert "set -euo pipefail" in manual[:release_head]
    assert "--confcutdir=" in manual
    assert "--import-mode=importlib" in manual
    for surface in ("岗位列表", "岗位详情", "岗位 CSV 导出", "审核工作台队列", "审核详情"):
        assert surface in manual
    assert release_head < release_clean < ancestor < stage_a_worktree
    assert stage_a_worktree < stage_a_deploy < stage_a_smoke < migration < final_deploy
    assert "禁止用最终 schema-aware 制品替代本阶段制品" in manual
    assert "最终制品只能在完整 Phase 10 schema 上启动" in manual


def test_release_manual_covers_complete_phase10_sequence_and_module_commands():
    manual = (ROOT.parent / "docs/岗位生命周期Phase10发布手册.md").read_text(
        encoding="utf-8"
    )

    for migration in (
        "phase10_001_job_visibility_fields.sql",
        "phase10_002_ensure_visibility_config.sql",
        "phase10_001_job_lifecycle_additive.sql",
        "phase10_002_media_dead_letter.sql",
        "phase10_003_session_commit_deadline.sql",
        "phase10_004_session_commit_lease_owner.sql",
    ):
        assert migration in manual
    visibility_fields = manual.index("phase10_001_job_visibility_fields.sql")
    visibility_config = manual.index("phase10_002_ensure_visibility_config.sql")
    lifecycle = manual.index("phase10_001_job_lifecycle_additive.sql")
    assert visibility_fields < visibility_config < lifecycle
    assert "禁止使用 `phase10_*.sql` glob" in manual
    for module in (
        "scripts.backfill_media_lifecycle",
        "scripts.backfill_target_cleanup_tasks",
        "scripts.phase10_clock_check",
        "scripts.phase10_preflight",
    ):
        assert f"python -m {module}" in manual
    assert "python scripts/" not in manual
    assert "2 秒" in manual
    for flag in (
        "JOB_REPLACEMENT_ENABLED",
        "JOB_EXPIRY_CLEANUP_ENABLED",
        "JOB_CANDIDATE_CLEANUP_ENABLED",
        "JOB_HARD_DELETE_ENABLED",
    ):
        assert flag in manual
    for topic in ("迁移", "媒体回填", "Target cleanup 回填", "监控", "回滚"):
        assert topic in manual


def test_release_manual_uses_one_verified_mysql_target_and_orders_backup_evidence():
    manual = (ROOT.parent / "docs/岗位生命周期Phase10发布手册.md").read_text(
        encoding="utf-8"
    )

    assert '--defaults-extra-file="$PHASE10_MYSQL_DEFAULTS_FILE"' in manual
    assert "SELECT DATABASE(), @@hostname, @@port" in manual
    assert "phase10_down_001_job_lifecycle.sql" in manual
    assert manual.count("set -euo pipefail") >= 2
    migration = manual.index("phase10_001_job_lifecycle_additive.sql")
    backup_evidence = manual.index("backup rows/checksum")
    archived_evidence = manual.index("phase10-001-backup-evidence.tsv")
    down_evidence = manual.index("phase10-down-backup-evidence.tsv")
    down_approval = manual.index("审批完成后才单独执行")
    down_command = manual.index("cat sql/migrations/phase10_down_001_job_lifecycle.sql")

    assert migration < backup_evidence
    assert archived_evidence < down_evidence < down_approval < down_command
    assert "cmp --silent phase10-001-backup-evidence.tsv" in manual
    assert "新证据与 `phase10-001-backup-evidence.tsv` 精确比较" in manual
    assert "新证据与 `phase10-001-output.txt` 精确比较" not in manual
    assert "@phase10_archived_expected_live_checksum" in manual
    for privilege in ("CREATE ROUTINE", "ALTER ROUTINE", "EXECUTE", "TRIGGER"):
        assert privilege in manual


def test_release_manual_restarts_and_verifies_all_phase10_processes():
    manual = (ROOT.parent / "docs/岗位生命周期Phase10发布手册.md").read_text(
        encoding="utf-8"
    )

    deployment = manual[
        manual.index("## 6. 最终版本部署"):manual.index("## 7. 开关顺序")
    ]
    for process in ("API", "消息 Worker", "scheduler", "session recovery Worker"):
        assert process in deployment
    assert "heartbeat" in deployment
    assert "镜像 digest" in deployment


def test_release_manual_stops_candidate_producer_before_consumer():
    manual = (ROOT.parent / "docs/岗位生命周期Phase10发布手册.md").read_text(
        encoding="utf-8"
    )
    rollback = manual[manual.index("## 9. 回滚"):]
    stop_producer = rollback.index("同步关闭 `JOB_REPLACEMENT_ENABLED`")
    keep_consumer = rollback.index("保持 `JOB_CANDIDATE_CLEANUP_ENABLED=true`")
    stop_consumer = rollback.index("才同步关闭 `JOB_CANDIDATE_CLEANUP_ENABLED`")

    assert stop_producer < keep_consumer < stop_consumer
    assert "media/target cleanup backlog 均收敛" in rollback


def test_release_manual_uses_dedicated_gate_after_destructive_down():
    manual = (ROOT.parent / "docs/岗位生命周期Phase10发布手册.md").read_text(
        encoding="utf-8"
    )
    rollback = manual[manual.index("## 9. 回滚"):]
    destructive = rollback[rollback.index("已经执行 destructive schema down"):]

    assert "不得运行 `python -m scripts.phase10_preflight`" in destructive
    assert "python -m scripts.phase10_down_verify" in destructive
    assert "499eb929b75ad2f208d306b62157d8ded0119f33" in destructive
    assert "old_job_table_contract_mismatch" in destructive
    assert "old_inbound_table_contract_mismatch" in destructive
    assert "old_inbound_constraints_mismatch" in destructive
    assert "old_inbound_triggers_remaining" in destructive
    assert "old_inbound_column_contract_mismatch" in destructive
    assert "old_inbound_index_contract_mismatch" in destructive
    assert "额外普通非唯一索引仅允许" in destructive
    assert "prefix、expression、无物理列或其他索引类型都会阻断" in destructive
    for surface in (
        "岗位列表第 1/2 页",
        "岗位详情",
        "岗位 CSV 导出",
        "审核工作台队列",
        "审核详情",
    ):
        assert surface in destructive


def test_down_verify_reports_only_zero_blockers_as_ready():
    metadata_visibility_gate = phase10_down_verify.SCHEMA_CHECKS[
        "down_verify_global_select_privilege_missing"
    ]
    assert "USER_PRIVILEGES" in metadata_visibility_gate
    assert "CURRENT_USER()" in metadata_visibility_gate
    assert "PRIVILEGE_TYPE='SELECT'" in metadata_visibility_gate
    trigger_visibility_gate = phase10_down_verify.SCHEMA_CHECKS[
        "down_verify_trigger_privilege_missing"
    ]
    assert "USER_PRIVILEGES" in trigger_visibility_gate
    assert "SCHEMA_PRIVILEGES" in trigger_visibility_gate
    assert "TABLE_SCHEMA=DATABASE()" in trigger_visibility_gate
    assert "PRIVILEGE_TYPE='TRIGGER'" in trigger_visibility_gate
    required_table_gate = phase10_down_verify.SCHEMA_CHECKS[
        "old_schema_required_tables_missing"
    ]
    assert "TABLE_TYPE='BASE TABLE'" in required_table_gate
    assert "BINARY TABLE_NAME" in required_table_gate
    job_table_gate = phase10_down_verify.SCHEMA_CHECKS[
        "old_job_table_contract_mismatch"
    ]
    assert "TABLE_TYPE='BASE TABLE'" in job_table_gate
    assert "ENGINE='InnoDB'" in job_table_gate
    assert "CHARACTER_SET_NAME='utf8mb4'" in job_table_gate
    assert "TABLE_COLLATION='utf8mb4_0900_ai_ci'" in job_table_gate
    inbound_table_gate = phase10_down_verify.SCHEMA_CHECKS[
        "old_inbound_table_contract_mismatch"
    ]
    assert "ENGINE='InnoDB'" in inbound_table_gate
    assert "CHARACTER_SET_NAME='utf8mb4'" in inbound_table_gate
    assert "TABLE_COLLATION='utf8mb4_0900_ai_ci'" in inbound_table_gate
    inbound_constraints_gate = phase10_down_verify.SCHEMA_CHECKS[
        "old_inbound_constraints_mismatch"
    ]
    assert "TABLE_CONSTRAINTS" in inbound_constraints_gate
    assert "'CHECK','FOREIGN KEY'" in inbound_constraints_gate
    inbound_referencing_fk_gate = phase10_down_verify.SCHEMA_CHECKS[
        "old_inbound_referencing_foreign_keys_mismatch"
    ]
    assert "KEY_COLUMN_USAGE" in inbound_referencing_fk_gate
    assert "REFERENCED_TABLE_SCHEMA=DATABASE()" in inbound_referencing_fk_gate
    assert "REFERENCED_TABLE_NAME='wecom_inbound_event'" in inbound_referencing_fk_gate
    inbound_trigger_gate = phase10_down_verify.SCHEMA_CHECKS[
        "old_inbound_triggers_remaining"
    ]
    assert "EVENT_OBJECT_TABLE='wecom_inbound_event'" in inbound_trigger_gate
    assert "TRIGGER_NAME" not in inbound_trigger_gate
    inbound_column_gate = phase10_down_verify.SCHEMA_CHECKS[
        "old_inbound_column_contract_mismatch"
    ]
    assert "COLUMN_TYPE" in inbound_column_gate
    assert "IS_NULLABLE" in inbound_column_gate
    assert "COLUMN_DEFAULT" in inbound_column_gate
    assert "CHARACTER_SET_NAME" in inbound_column_gate
    assert "COLLATION_NAME" in inbound_column_gate
    assert "GENERATION_EXPRESSION" in inbound_column_gate
    assert "auto_increment" in inbound_column_gate
    assert "DEFAULT_GENERATED" in inbound_column_gate
    assert "NOT IN" in inbound_column_gate
    assert "LOWER(actual.COLUMN_TYPE)" not in inbound_column_gate
    inbound_index_gate = phase10_down_verify.SCHEMA_CHECKS[
        "old_inbound_index_contract_mismatch"
    ]
    assert "GROUP_CONCAT" in inbound_index_gate
    assert "SEQ_IN_INDEX" in inbound_index_gate
    assert "NON_UNIQUE" in inbound_index_gate
    assert "IS_VISIBLE" in inbound_index_gate
    assert "SUB_PART" in inbound_index_gate
    assert "EXPRESSION" in inbound_index_gate
    assert "COUNT(DISTINCT actual.INDEX_NAME)" in inbound_index_gate
    assert "actual.NON_UNIQUE=0" in inbound_index_gate
    assert "actual.NON_UNIQUE=1" in inbound_index_gate
    assert "actual.COLUMN_NAME IS NULL" in inbound_index_gate
    assert "actual.INDEX_TYPE<>'BTREE'" in inbound_index_gate
    job_column_gate = phase10_down_verify.SCHEMA_CHECKS[
        "old_job_column_contract_mismatch"
    ]
    assert "COLUMN_TYPE" in job_column_gate
    assert "GENERATION_EXPRESSION" in job_column_gate
    assert "expires_at" in job_column_gate
    assert "NOT IN" in job_column_gate
    job_index_gate = phase10_down_verify.SCHEMA_CHECKS[
        "old_job_index_contract_mismatch"
    ]
    assert "GROUP_CONCAT" in job_index_gate
    assert "IS_VISIBLE" in job_index_gate
    assert "NOT IN" in job_index_gate
    job_constraints_gate = phase10_down_verify.SCHEMA_CHECKS[
        "old_job_constraints_mismatch"
    ]
    assert "REFERENTIAL_CONSTRAINTS" in job_constraints_gate
    assert "ORDINAL_POSITION=1" in job_constraints_gate
    assert "POSITION_IN_UNIQUE_CONSTRAINT=1" in job_constraints_gate
    assert "REFERENCED_TABLE_NAME IS NOT NULL)=1" in job_constraints_gate
    assert "fk_job_owner" in job_constraints_gate
    assert "CONSTRAINT_TYPE='CHECK'" in job_constraints_gate
    job_trigger_gate = phase10_down_verify.SCHEMA_CHECKS[
        "old_job_triggers_remaining"
    ]
    assert "EVENT_OBJECT_TABLE='job'" in job_trigger_gate
    backup_key_gate = phase10_down_verify.SCHEMA_CHECKS[
        "backup_job_id_key_contract_mismatch"
    ]
    assert "TABLE_NAME='phase10_job_lifecycle_backup'" in backup_key_gate
    assert "INDEX_NAME='PRIMARY'" in backup_key_gate
    assert "columns='job_id'" in backup_key_gate
    assert "NON_UNIQUE=0" in backup_key_gate

    db = MagicMock()
    results = []
    for _ in phase10_down_verify.SCHEMA_CHECKS:
        result = MagicMock()
        result.scalar.return_value = 0
        results.append(result)
    required_tables = MagicMock()
    required_tables.scalar.return_value = 2
    restore_mismatch = MagicMock()
    restore_mismatch.scalar.return_value = 0
    duplicate_rows = MagicMock()
    duplicate_rows.scalar.return_value = 0
    row_count_mismatch = MagicMock()
    row_count_mismatch.scalar.return_value = 0
    current_account = MagicMock()
    current_account.scalar.return_value = "root@%"
    db.execute.side_effect = [
        current_account, *results, required_tables, restore_mismatch,
        duplicate_rows, row_count_mismatch,
    ]

    report = phase10_down_verify.collect(db, expected_account="root@%")

    assert report["ready"] is True
    assert report["down_verify_database_account_mismatch"] == 0
    assert report["restored_job_backup_mismatch"] == 0
    assert report["backup_duplicate_job_id_rows"] == 0
    assert report["restored_job_backup_row_count_mismatch"] == 0
    assert report["phase10_session_columns_remaining"] == 0
    assert report["old_schema_required_tables_missing"] == 0
    assert report["old_job_table_contract_mismatch"] == 0
    assert report["old_inbound_table_contract_mismatch"] == 0
    assert report["old_inbound_constraints_mismatch"] == 0
    assert report["old_inbound_referencing_foreign_keys_mismatch"] == 0
    assert report["old_inbound_triggers_remaining"] == 0
    assert report["old_inbound_column_contract_mismatch"] == 0
    assert report["old_inbound_index_contract_mismatch"] == 0

    required_table_result_index = list(phase10_down_verify.SCHEMA_CHECKS).index(
        "old_schema_required_tables_missing"
    )
    results[required_table_result_index].scalar.return_value = 1
    db.execute.side_effect = [
        current_account, *results, required_tables, restore_mismatch,
        duplicate_rows, row_count_mismatch,
    ]
    report = phase10_down_verify.collect(db, expected_account="root@%")
    assert report["old_schema_required_tables_missing"] == 1
    assert report["ready"] is False

    db.execute.side_effect = [
        current_account, *results, required_tables, restore_mismatch,
        duplicate_rows, row_count_mismatch,
    ]
    report = phase10_down_verify.collect(db, expected_account="migration@%")
    assert report["down_verify_database_account_mismatch"] == 1
    assert report["ready"] is False


def test_preflight_uses_distinct_job_and_candidate_ttl_ranges():
    job_gate = phase10_preflight.CHECKS["invalid_job_ttl_config"]
    candidate_gate = phase10_preflight.CHECKS["invalid_candidate_ttl_config"]

    assert "BETWEEN 1 AND 3650" in job_gate
    assert "BETWEEN 1 AND 365" in candidate_gate
    assert "BETWEEN 1 AND 3650" not in candidate_gate


def test_preflight_fails_if_dead_letter_enum_is_missing(monkeypatch):
    monkeypatch.setattr(
        phase10_preflight, "collect_media_coverage", lambda _: _clean_media_coverage()
    )
    db = MagicMock()
    scalar_results = [MagicMock() for _ in phase10_preflight.CHECKS]
    for result in scalar_results:
        result.scalar.return_value = 0
    enum_index = list(phase10_preflight.CHECKS).index(
        "media_state_enum_missing_dead_letter"
    )
    scalar_results[enum_index].scalar.return_value = 1
    auto_increment = MagicMock()
    auto_increment.one.return_value = (101, 100)
    db.execute.side_effect = [*scalar_results, auto_increment]

    result = phase10_preflight.collect(db)

    assert result["ready"] is False
    assert result["media_state_enum_missing_dead_letter"] == 1


def test_preflight_fails_on_any_invariant_or_invalid_auto_increment(monkeypatch):
    monkeypatch.setattr(
        phase10_preflight, "collect_media_coverage", lambda _: _clean_media_coverage()
    )
    db = MagicMock()
    scalar_results = [MagicMock() for _ in phase10_preflight.CHECKS]
    for result in scalar_results:
        result.scalar.return_value = 0
    passed_index = list(phase10_preflight.CHECKS).index("passed_without_activation")
    scalar_results[passed_index].scalar.return_value = 1
    auto_increment = MagicMock()
    auto_increment.one.return_value = (100, 100)
    db.execute.side_effect = [*scalar_results, auto_increment]

    result = phase10_preflight.collect(db)

    assert result["ready"] is False
    assert result["passed_without_activation"] == 1
    assert result["job_auto_increment_not_above_max"] == 1


def test_preflight_fails_on_media_coverage_blocker(monkeypatch):
    db = MagicMock()
    scalar_results = [MagicMock() for _ in phase10_preflight.CHECKS]
    for result in scalar_results:
        result.scalar.return_value = 0
    auto_increment = MagicMock()
    auto_increment.one.return_value = (101, 100)
    db.execute.side_effect = [*scalar_results, auto_increment]
    media = _clean_media_coverage()
    media["missing_media_lifecycle_key_count"] = 1
    monkeypatch.setattr(phase10_preflight, "collect_media_coverage", lambda _: media)

    result = phase10_preflight.collect(db)

    assert result["ready"] is False
    assert result["missing_media_lifecycle_key_count"] == 1


def test_rollout_feature_defaults_are_fail_closed():
    from app.config import Settings

    for name in (
        "job_replacement_enabled",
        "job_expiry_cleanup_enabled",
        "job_candidate_cleanup_enabled",
        "job_hard_delete_enabled",
    ):
        assert Settings.model_fields[name].default is False

    env_example = (ROOT.parent / ".env.example").read_text(encoding="utf-8")
    for name in (
        "JOB_REPLACEMENT_ENABLED",
        "JOB_EXPIRY_CLEANUP_ENABLED",
        "JOB_CANDIDATE_CLEANUP_ENABLED",
        "JOB_HARD_DELETE_ENABLED",
    ):
        assert f"{name}=false" in env_example


def test_phase10_ci_runs_complete_real_mysql_and_redis_gate():
    workflow = (ROOT.parent / ".github/workflows/backend-ci.yml").read_text(
        encoding="utf-8"
    )
    start = workflow.index("  backend-phase10-mysql-redis:")
    end = workflow.index("  backend-recommendation-mysql:", start)
    job = workflow[start:end]

    assert "DB_USER: root" in job
    assert "jobbridge_test < sql/schema.sql" in job
    assert "REDIS_OUTAGE_CONTAINER: jobbridge-phase10-ci-redis" in job
    for policy in (
        "--appendonly yes",
        "--appendfsync always",
        "--maxmemory-policy noeviction",
    ):
        assert policy in job
    required_tests = (
        "test_search_sql_mysql.py",
        "test_phase10_preflight_mysql.py",
        "test_phase10_down_migration_mysql.py",
        "test_job_candidate_creation_gate_mysql.py",
        "test_job_media_hard_delete_delay_mysql.py",
        "test_job_replace_mysql.py",
        "test_media_target_cleanup_lock_order_mysql.py",
        "test_outbox_claim_lock_scope_mysql.py",
        "test_privacy_lock_order_mysql.py",
        "test_privacy_redaction_batch_mysql.py",
        "test_session_commit_deadline_mysql.py",
        "test_session_commit_lease_owner_mysql.py",
        "test_target_cleanup_backfill_mysql.py",
        "test_target_cleanup_checkpoint_redis_mysql.py",
        "test_target_cleanup_lease_mysql.py",
        "test_target_cleanup_upsert_mysql.py",
        "test_ttl_outbox_delivery_lock_order_mysql.py",
        "test_redis.py",
        "test_session_commit_redis_unavailable_mysql.py",
    )
    for test_file in required_tests:
        assert job.count(test_file) == 1
    assert job.index("test_redis.py") < job.index(
        "test_session_commit_redis_unavailable_mysql.py"
    )


def test_local_media_route_and_shared_directory_contracts():
    from fastapi.staticfiles import StaticFiles
    from app.main import app

    route = next(route for route in app.routes if getattr(route, "path", None) == "/files")
    assert isinstance(route.app, StaticFiles)

    production_compose = (ROOT.parent / "docker-compose.prod.yml").read_text(
        encoding="utf-8"
    )
    assert production_compose.count("OSS_LOCAL_DIR=/data/uploads") == 2
    assert production_compose.count("app_uploads:/data/uploads") == 2

    development_compose = (ROOT.parent / "docker-compose.yml").read_text(
        encoding="utf-8"
    )
    assert "./backend/uploads:/app/uploads" in development_compose
    assert "worker_uploads:/app/uploads" not in development_compose
