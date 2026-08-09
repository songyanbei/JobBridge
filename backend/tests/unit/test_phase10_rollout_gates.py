from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from scripts import phase10_preflight


ROOT = Path(__file__).resolve().parents[2]


def test_additive_migration_contains_rollout_invariants():
    sql = (ROOT / "sql/migrations/phase10_001_job_lifecycle_additive.sql").read_text(
        encoding="utf-8"
    )
    assert "phase10_job_lifecycle_backup" in sql
    assert "AUTO_INCREMENT" in sql and "MAX(j.`id`)" in sql
    assert "backup_checksum" in sql
    assert "audit_status` = 'passed'" in sql
    assert "candidate_expires_at` IS NULL" in sql


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


def test_preflight_reports_all_clean_database_as_ready():
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
    assert "invalid_job_ttl_config" in phase10_preflight.CHECKS
    assert "invalid_candidate_ttl_config" in phase10_preflight.CHECKS
    assert "job_backup_coverage_mismatch" in phase10_preflight.CHECKS


def test_preflight_fails_on_any_invariant_or_invalid_auto_increment():
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
