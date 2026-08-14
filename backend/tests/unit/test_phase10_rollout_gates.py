from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts import phase10_preflight


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
    assert "phase10_down_blocked" in sql
    assert "COUNT(*) FROM `phase10_job_lifecycle_backup`" in sql
    assert "phase10_restore_checksum_valid" in sql
    assert "phase10_down_guard_failed_checksum_mismatch" in sql


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
    enum_gate = phase10_preflight.CHECKS["media_state_enum_missing_dead_letter"]
    assert "information_schema.COLUMNS" in enum_gate
    assert "COLUMN_TYPE" in enum_gate
    assert "dead_letter" in enum_gate


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


def test_release_manual_covers_complete_phase10_sequence_and_module_commands():
    manual = (ROOT.parent / "docs/岗位生命周期Phase10发布手册.md").read_text(
        encoding="utf-8"
    )

    for migration in (
        "phase10_001_job_lifecycle_additive.sql",
        "phase10_002_media_dead_letter.sql",
        "phase10_003_session_commit_deadline.sql",
        "phase10_004_session_commit_lease_owner.sql",
    ):
        assert migration in manual
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
    down_evidence = manual.index("phase10-down-backup-evidence.txt")
    down_approval = manual.index("审批完成后才单独执行")
    down_command = manual.index(
        "< sql/migrations/phase10_down_001_job_lifecycle.sql"
    )

    assert migration < backup_evidence
    assert down_evidence < down_approval < down_command


def test_release_manual_restarts_and_verifies_all_phase10_processes():
    manual = (ROOT.parent / "docs/岗位生命周期Phase10发布手册.md").read_text(
        encoding="utf-8"
    )

    deployment = manual[manual.index("## 5. 同版本部署"):manual.index("## 6. 开关顺序")]
    for process in ("API", "消息 Worker", "scheduler", "session recovery Worker"):
        assert process in deployment
    assert "heartbeat" in deployment
    assert "镜像 digest" in deployment


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
