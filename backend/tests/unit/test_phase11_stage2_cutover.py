import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.config import Settings, settings
from app.core.exceptions import BusinessException
from app.main import health_check
from app.llm.base import IntentResult
from app.schemas.conversation import SessionState
from app.services import audit_workbench_service, resume_admin_service, upload_service
from app.services.phase11_build_info import PHASE11_CAPABILITIES, build_probe_payload
from app.services.resume_cutover_service import (
    assert_lifecycle_invariants,
    assert_resume_writes_allowed,
    capture_cutover_watermark,
    lifecycle_anomaly_counts,
)


def test_build_settings_are_fail_closed_and_validate_contract():
    configured = Settings(
        _env_file=None,
        phase11_build_number=42,
        phase11_build_sha="A" * 40,
    )
    assert configured.phase11_build_number == 42
    assert configured.phase11_build_sha == "a" * 40
    with pytest.raises(ValueError):
        Settings(_env_file=None, phase11_build_number=-1)
    with pytest.raises(ValueError):
        Settings(_env_file=None, phase11_build_sha="not-a-sha")


def test_api_health_exposes_runner_build_contract(monkeypatch):
    monkeypatch.setattr(settings, "phase11_build_number", 17)
    monkeypatch.setattr(settings, "phase11_build_sha", "b" * 40)
    payload = health_check()
    assert payload["build_number"] == 17
    assert payload["build_sha"] == "b" * 40
    assert set(payload["capabilities"]) == set(PHASE11_CAPABILITIES)


def test_worker_heartbeat_contains_same_build_contract(monkeypatch):
    from app.services.worker import Worker

    monkeypatch.setattr(settings, "phase11_build_number", 18)
    monkeypatch.setattr(settings, "phase11_build_sha", "c" * 40)
    worker = Worker.__new__(Worker)
    worker._running = True
    worker._pid = 123
    worker._redis = MagicMock()

    def stop_after_write(*_args, **_kwargs):
        worker._running = False

    worker._redis.set.side_effect = stop_after_write
    worker._start_heartbeat()
    worker._heartbeat_thread.join(timeout=3)

    value = worker._redis.set.call_args.args[1]
    payload = json.loads(value)
    assert payload == build_probe_payload()


def test_write_barrier_and_watermark_require_explicit_pause(monkeypatch):
    db = MagicMock()
    db.query.return_value.scalar.return_value = 77
    monkeypatch.setattr(settings, "phase11_resume_writes_paused", False)
    assert_resume_writes_allowed()
    with pytest.raises(RuntimeError, match="barrier_not_active"):
        capture_cutover_watermark(db)

    monkeypatch.setattr(settings, "phase11_resume_writes_paused", True)
    with pytest.raises(BusinessException) as exc:
        assert_resume_writes_allowed()
    assert exc.value.message == "resume_writes_paused_for_cutover"
    assert capture_cutover_watermark(db) == 77


@pytest.mark.parametrize(
    "writer",
    [
        lambda db: upload_service.process_upload(
            SimpleNamespace(role="worker"),
            IntentResult(intent="upload_resume", structured_data={}),
            "resume text",
            [],
            SessionState(role="worker"),
            db,
        ),
        lambda db: audit_workbench_service._pass_resume(db, 7, 1, "admin-1"),
        lambda db: resume_admin_service.update_resume(
            db, 7, 1, {"description": "new"}, "admin-1",
        ),
        lambda db: audit_workbench_service.edit_action(
            db, "resume", 7, 1, {"description": "new"}, "admin-1",
        ),
    ],
    ids=("upload", "manual-review", "resume-admin-edit", "audit-workbench-edit"),
)
def test_stop_write_barrier_is_enforced_at_each_resume_writer(monkeypatch, writer):
    monkeypatch.setattr(settings, "phase11_resume_writes_paused", True)
    db = MagicMock()

    with pytest.raises(BusinessException) as exc:
        writer(db)

    assert exc.value.message == "resume_writes_paused_for_cutover"
    db.query.assert_not_called()
    db.commit.assert_not_called()


def test_manifest_anchor_pins_the_pre_anchor_compatibility_commit():
    manifest = Path(__file__).parents[2] / "sql" / "migrations" / "phase11_manifest.json"
    minimum_build = json.loads(manifest.read_text(encoding="utf-8"))["minimum_build"]
    assert minimum_build == {
        "ready": True,
        "build_number": 249,
        "build_sha": "083be7e8fa37045a94f5247ea4fb9cb8d1a35652",
        "capabilities": list(PHASE11_CAPABILITIES),
    }


def test_incremental_invariant_check_is_scoped_after_watermark():
    db = MagicMock()
    first = MagicMock()
    first.filter.return_value.scalar.return_value = 0
    second = MagicMock()
    second.filter.return_value.scalar.return_value = 0
    db.query.side_effect = [first, second]

    assert assert_lifecycle_invariants(db, after_id=77) == {
        "passed_invalid": 0,
        "candidate_invalid": 0,
    }
    assert "resume.id" in str(first.filter.call_args.args[0]).lower()


def test_incremental_invariant_check_fails_on_new_invalid_writer_output():
    db = MagicMock()
    first = MagicMock()
    first.filter.return_value.scalar.return_value = 1
    second = MagicMock()
    second.filter.return_value.scalar.return_value = 0
    db.query.side_effect = [first, second]
    assert lifecycle_anomaly_counts(db, after_id=10)["passed_invalid"] == 1

    db.query.side_effect = [first, second]
    with pytest.raises(RuntimeError, match="incremental_invariant_failed"):
        assert_lifecycle_invariants(db, after_id=10)
