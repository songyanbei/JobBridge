from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from scripts import phase10_preflight


ROOT = Path(__file__).resolve().parents[2]


def _clean_media_coverage():
    return {name: 0 for name in phase10_preflight.MEDIA_REPORT_FIELDS}


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
    assert "invalid_job_ttl_config" in phase10_preflight.CHECKS
    assert "invalid_candidate_ttl_config" in phase10_preflight.CHECKS
    assert "job_backup_coverage_mismatch" in phase10_preflight.CHECKS


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
