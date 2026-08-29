from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.core.exceptions import BusinessException
from app.models import Resume
from app.services.audit_workbench_service import _pass_resume, _reject_resume
from app.services.resume_activation_service import activate_resume
from app.services.upload_service import _create_resume


NOW = datetime(2026, 8, 17, 1, 2, 3, 456789)


def _resume(**overrides) -> Resume:
    values = {
        "id": 7,
        "owner_userid": "worker-1",
        "expected_cities": ["苏州"],
        "expected_job_categories": ["电子厂"],
        "salary_expect_floor_monthly": 5000,
        "gender": "男",
        "age": 30,
        "raw_text": "完整简历",
        "audit_status": "pending",
        "candidate_expires_at": NOW + timedelta(days=7),
        "version": 1,
    }
    values.update(overrides)
    return Resume(**values)


def _data() -> dict:
    return {
        "expected_cities": ["苏州"],
        "expected_job_categories": ["电子厂"],
        "salary_expect_floor_monthly": 5000,
        "gender": "男",
        "age": 30,
    }


@patch("app.services.resume_activation_service.get_resume_ttl_days", return_value=30)
def test_activate_resume_uses_one_naive_utc_instant_and_is_idempotent(_ttl):
    db = MagicMock()
    row = _resume()

    activate_resume(db, row, now=NOW.replace(tzinfo=timezone.utc))

    assert row.audit_status == "passed"
    assert row.activated_at == NOW
    assert row.expires_at == NOW + timedelta(days=30)
    assert row.candidate_expires_at is None
    assert row.version == 2

    activate_resume(db, row, now=NOW + timedelta(days=3))
    assert row.activated_at == NOW
    assert row.expires_at == NOW + timedelta(days=30)
    assert row.version == 2
    assert db.flush.call_count == 1


@pytest.mark.parametrize("status", ["pending", "rejected"])
@patch("app.services.lifecycle_config_service.get_resume_candidate_ttl_days", return_value=7)
def test_first_publish_candidate_has_no_business_ttl(_candidate_ttl, status):
    db = MagicMock()
    audit = SimpleNamespace(status=status, reason="review")
    row = _create_resume(
        _data(), SimpleNamespace(external_userid="worker-1"), audit,
        30, "完整简历", [], db,
    )

    assert row.audit_status == status
    assert row.activated_at is None
    assert row.expires_at is None
    assert row.candidate_expires_at is not None
    assert row.candidate_expires_at.tzinfo is None


@patch("app.services.resume_activation_service.get_resume_ttl_days", return_value=30)
@patch("app.services.lifecycle_config_service.get_resume_candidate_ttl_days", return_value=7)
def test_auto_pass_first_publish_activates_even_when_rollout_flags_are_off(
    _candidate_ttl, _ttl, monkeypatch,
):
    from app.config import settings

    monkeypatch.setattr(settings, "resume_lifecycle_v2_enabled", False)
    db = MagicMock()
    row = _create_resume(
        _data(), SimpleNamespace(external_userid="worker-1"),
        SimpleNamespace(status="passed", reason=""), 30, "完整简历", [], db,
    )
    assert row.audit_status == "passed"
    assert row.activated_at is not None
    assert row.expires_at == row.activated_at + timedelta(days=30)
    assert row.candidate_expires_at is None


@patch("app.services.audit_workbench_service.write_admin_log")
@patch("app.services.resume_activation_service.get_resume_ttl_days", return_value=30)
@patch("app.services.resume_mutation_service.utc_now_naive", return_value=NOW)
@patch("app.services.resume_mutation_service.lock_resume")
def test_manual_pass_restarts_full_ttl_and_commits_locked_version(
    lock, _now, _ttl, _log,
):
    db = MagicMock()
    row = _resume(candidate_expires_at=NOW + timedelta(days=4))
    lock.return_value = row

    _pass_resume(db, row.id, 1, "admin-1")

    assert row.activated_at == NOW
    assert row.expires_at == NOW + timedelta(days=30)
    assert row.version == 2
    db.commit.assert_called_once()


@patch("app.services.audit_workbench_service.write_admin_log")
@patch("app.services.resume_activation_service.get_resume_ttl_days", return_value=30)
@patch("app.services.resume_mutation_service.utc_now_naive", return_value=NOW)
@patch("app.services.resume_mutation_service.lock_resume")
def test_compatible_manual_pass_repairs_legacy_pending_lifecycle(
    lock, _now, _ttl, _log, monkeypatch,
):
    from app.config import settings

    monkeypatch.setattr(settings, "resume_lifecycle_v2_enabled", False)
    db = MagicMock()
    lock.return_value = _resume(
        activated_at=None,
        expires_at=NOW + timedelta(days=20),
        candidate_expires_at=None,
    )
    _pass_resume(db, 7, 1, "admin-1")
    assert lock.return_value.activated_at == NOW
    assert lock.return_value.expires_at == NOW + timedelta(days=30)


@patch("app.services.resume_mutation_service.lock_resume")
def test_manual_pass_loses_same_version_race_without_writing(lock):
    db = MagicMock()
    lock.return_value = _resume(version=2)
    with pytest.raises(BusinessException) as exc:
        _pass_resume(db, 7, 1, "admin-1")
    assert exc.value.code == 40902
    db.commit.assert_not_called()


@patch("app.services.resume_mutation_service.utc_now_naive", return_value=NOW)
@patch("app.services.resume_mutation_service.lock_resume")
def test_manual_pass_rejects_expired_candidate_before_writing(lock, _now):
    db = MagicMock()
    lock.return_value = _resume(candidate_expires_at=NOW)

    with pytest.raises(BusinessException) as exc:
        _pass_resume(db, 7, 1, "admin-1")

    assert exc.value.message == "candidate_expired"
    db.flush.assert_not_called()
    db.commit.assert_not_called()


@patch("app.services.audit_workbench_service.write_admin_log")
@patch("app.services.resume_mutation_service.utc_now_naive", return_value=NOW)
@patch("app.services.resume_mutation_service.lock_resume")
def test_manual_reject_keeps_candidate_ttl_and_has_no_undo(lock, _now, _log):
    db = MagicMock()
    row = _resume()
    lock.return_value = row
    candidate_deadline = row.candidate_expires_at

    _reject_resume(db, row.id, 1, "内容不完整", "admin-1", block_user=False)

    assert row.audit_status == "rejected"
    assert row.expires_at is None
    assert row.candidate_expires_at == candidate_deadline
    assert row.version == 2
    db.commit.assert_called_once()
