from datetime import datetime, timedelta
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import MetaData, create_engine
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.core.exceptions import BusinessException
from app.models import (
    Resume, ResumeReplacement, SystemConfig, TargetCleanupTask, User,
)
from app.services import (
    audit_workbench_service, resume_admin_service, resume_mutation_service,
    resume_replace_service,
)
from app.services.resume_business_digest_service import business_digest
from app.services.target_cleanup_service import (
    ensure_target_cleanup_task, target_cleanup_succeeded,
)


@compiles(mysql.TINYINT, "sqlite")
@compiles(mysql.SMALLINT, "sqlite")
@compiles(mysql.INTEGER, "sqlite")
@compiles(mysql.BIGINT, "sqlite")
def _compile_mysql_integer_for_sqlite(_type, _compiler, **_kwargs):
    return "INTEGER"


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    for source in (
        User.__table__, Resume.__table__, ResumeReplacement.__table__,
        TargetCleanupTask.__table__, SystemConfig.__table__,
    ):
        table = source.to_metadata(metadata)
        table.indexes.clear()
    metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(User(external_userid="worker-1", role="worker"))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _resume(db, *, active: bool, version: int = 1, raw_text: str = "完整简历"):
    now = datetime.utcnow()
    row = Resume(
        owner_userid="worker-1", expected_cities=["苏州"],
        expected_job_categories=["普工"], salary_expect_floor_monthly=5000,
        gender="男", age=30, accept_long_term=True, accept_short_term=False,
        raw_text=raw_text, audit_status="passed" if active else "pending",
        activated_at=now - timedelta(days=1) if active else None,
        expires_at=now + timedelta(days=20) if active else None,
        candidate_expires_at=None if active else now + timedelta(days=7),
        version=version,
    )
    db.add(row)
    db.flush()
    return row


def _graph(db, *, old_version=2):
    old = _resume(db, active=True, version=old_version)
    new = _resume(db, active=False)
    relation = ResumeReplacement(
        operation_id="00000000-0000-0000-0000-000000000001",
        source_msg_id="stage4-message", owner_userid="worker-1",
        old_resume_id=old.id, new_resume_id=new.id,
        old_resume_version=old.version, old_expires_at=old.expires_at,
        old_business_digest=business_digest(old), old_business_digest_version=1,
        review_outcome="passed", lifecycle_status="awaiting_review",
        active_old_resume_id=old.id,
    )
    db.add(relation)
    db.flush()
    return relation, old, new


def test_unit_a_exact_base_activates_and_creates_cleanup_in_same_transaction(db):
    relation, old, new = _graph(db)

    assert resume_replace_service.activate_replacement_locked(
        db, relation, old, new, expected_old_version=2,
    ) is True

    assert new.audit_status == "passed" and new.activated_at is not None
    assert old.delist_reason == "replaced" and old.version == 3
    assert relation.lifecycle_status == "activated" and relation.active_old_resume_id is None
    task = db.query(TargetCleanupTask).filter_by(target_type="resume", target_id=old.id).one()
    assert task.reason == "replaced"


def test_unit_a_replacement_events_use_old_and_new_targets(db, monkeypatch):
    relation, old, new = _graph(db)
    append = MagicMock()
    monkeypatch.setitem(sys.modules, "app.services.domain_outbox_service", SimpleNamespace(append_domain_event=append))
    monkeypatch.setattr(resume_replace_service, "_domain_outbox_available", lambda _db: True)
    assert resume_replace_service.activate_replacement_locked(
        db, relation, old, new, expected_old_version=2,
    ) is True
    assert [call.kwargs["aggregate_id"] for call in append.call_args_list] == [old.id, new.id]
    assert append.call_args_list[0].kwargs["event_type"] == "resume.replaced"
    assert append.call_args_list[1].kwargs["event_type"] == "resume.updated"


def test_unit_a_digest_change_is_stable_conflict_without_switch(db):
    relation, old, new = _graph(db)
    old.raw_text = "管理员已修改"

    assert resume_replace_service.activate_replacement_locked(
        db, relation, old, new, expected_old_version=2,
    ) is False

    assert relation.lifecycle_status == "conflict"
    assert relation.conflict_reason == "replacement_conflict"
    assert old.deleted_at is None and new.activated_at is None


def test_unit_a_only_base_plus_one_natural_expiry_preserves_old_lifecycle(db):
    relation, old, new = _graph(db)
    expired_at = datetime.utcnow()
    old.version = 3
    old.delist_reason = "expired"
    old.deleted_at = expired_at

    assert resume_replace_service.activate_replacement_locked(
        db, relation, old, new, expected_old_version=2,
    ) is True
    assert old.version == 3 and old.delist_reason == "expired" and old.deleted_at == expired_at
    assert new.activated_at is not None


def test_unit_a_cleanup_failure_rolls_back_old_new_and_relation(db, monkeypatch):
    relation, old, new = _graph(db)
    relation_id, old_id, new_id = relation.id, old.id, new.id
    db.commit()
    relation.old_business_digest = business_digest(old)
    relation.old_expires_at = old.expires_at
    db.commit()

    def fail_cleanup(*_args, **_kwargs):
        raise RuntimeError("injected_cleanup_failure")

    monkeypatch.setattr(resume_replace_service, "ensure_target_cleanup_task", fail_cleanup)
    with pytest.raises(RuntimeError, match="injected_cleanup_failure"):
        resume_replace_service.activate_replacement_locked(
            db, relation, old, new, expected_old_version=2,
        )
    db.rollback()

    persisted_old = db.get(Resume, old_id)
    persisted_new = db.get(Resume, new_id)
    persisted_relation = db.get(ResumeReplacement, relation_id)
    assert persisted_old.deleted_at is None and persisted_old.delist_reason is None
    assert persisted_new.activated_at is None and persisted_new.audit_status == "pending"
    assert persisted_relation.lifecycle_status == "awaiting_review"


def test_unit_a_activation_reuses_caller_utc_in_every_lifecycle_write(db):
    relation, old, new = _graph(db)
    reviewed_at = datetime(2026, 8, 18, 3, 4, 5, 678901)
    relation.reviewed_at = reviewed_at
    new.audited_at = reviewed_at

    assert resume_replace_service.activate_replacement_locked(
        db, relation, old, new, expected_old_version=2, now=reviewed_at,
    ) is True

    assert relation.reviewed_at == relation.activated_at == reviewed_at
    assert new.audited_at == new.activated_at == reviewed_at
    assert old.deleted_at == reviewed_at


def test_unit_a_manual_conflict_keeps_operator_and_single_review_time(db, monkeypatch):
    relation, old, candidate = _graph(db)
    relation.review_outcome = "pending"
    old.raw_text = "审核前已修改"
    reviewed_at = datetime(2026, 8, 18, 6, 7, 8, 901234)
    monkeypatch.setattr(resume_mutation_service, "utc_now_naive", lambda: reviewed_at)
    monkeypatch.setattr(audit_workbench_service, "write_admin_log", lambda *a, **k: None)

    audit_workbench_service._pass_resume(
        db, candidate.id, candidate.version, "operator-7",
    )

    assert relation.lifecycle_status == "conflict"
    assert relation.reviewed_by == candidate.audited_by == "operator-7"
    assert relation.reviewed_at == candidate.audited_at == reviewed_at
    assert relation.activated_at is None and candidate.activated_at is None


def test_unit_b_generic_cleanup_wrapper_accepts_resume_and_reports_success(db):
    resume = _resume(db, active=True)
    first = ensure_target_cleanup_task(db, "resume", resume.id, reason="manual_delist")
    second = ensure_target_cleanup_task(db, "resume", resume.id, reason="user_deleted")
    assert first.id == second.id
    assert second.reason_history == ["manual_delist", "user_deleted"]
    assert target_cleanup_succeeded(db, "resume", resume.id) is False
    second.status = "succeeded"
    db.flush()
    assert target_cleanup_succeeded(db, "resume", resume.id) is True


def test_unit_c_extension_uses_activation_ceiling_and_stable_limit_error(db, monkeypatch):
    monkeypatch.setattr(resume_admin_service, "write_admin_log", lambda *a, **k: None)
    resume = _resume(db, active=True, version=1)
    now = datetime.utcnow()
    resume.activated_at = now - timedelta(days=10)
    resume.expires_at = now + timedelta(days=20)
    db.add(SystemConfig(config_key="ttl.resume.days", config_value="30"))
    db.commit()

    updated = resume_admin_service.extend(db, resume.id, 1, 30, "operator-1")
    ceiling = resume.activated_at + timedelta(days=60)
    assert updated.expires_at == ceiling

    with pytest.raises(BusinessException) as exc:
        resume_admin_service.extend(db, resume.id, 2, 15, "operator-1")
    assert exc.value.message == "extension_limit_reached"


def test_unit_a_auto_pass_candidate_enters_atomic_activation(db, monkeypatch):
    old = _resume(db, active=True, version=4)
    db.commit()
    monkeypatch.setattr("app.config.settings.resume_replacement_enabled", True)
    monkeypatch.setattr(
        resume_replace_service,
        "lock_replacement_creation",
        lambda *_args, **_kwargs: (None, old),
    )
    monkeypatch.setattr(
        resume_replace_service, "get_resume_candidate_ttl_days", lambda _db: 7,
    )
    activated = []

    def record_activation(
        _db, relation, previous, candidate, *, expected_old_version, now,
    ):
        activated.append((relation, previous, candidate, expected_old_version, now))
        return True

    monkeypatch.setattr(
        resume_replace_service, "activate_replacement_locked", record_activation,
    )
    relation, candidate = resume_replace_service.create_replacement_candidate(
        db,
        owner_userid="worker-1",
        target_resume_id=old.id,
        expected_version=4,
        operation_id="00000000-0000-0000-0000-000000000099",
        source_msg_id="stage4-auto-pass",
        complete_data={
            "expected_cities": ["苏州"],
            "expected_job_categories": ["普工"],
            "salary_expect_floor_monthly": 6000,
            "gender": "男",
            "age": 31,
        },
        raw_text="自动审核通过的新简历",
        media_ids=[],
        audit_result=type("Audit", (), {"status": "passed", "reason": ""})(),
    )

    assert relation.review_outcome == "passed"
    assert candidate.audit_status == "pending"
    assert relation.reviewed_at == candidate.audited_at
    assert activated == [(relation, old, candidate, 5, relation.reviewed_at)]


def test_unit_a_replacement_binds_media_to_candidate_version(db, monkeypatch):
    old = _resume(db, active=True, version=4)
    db.commit()
    monkeypatch.setattr("app.config.settings.resume_replacement_enabled", True)
    monkeypatch.setattr(
        resume_replace_service,
        "lock_replacement_creation",
        lambda *_args, **_kwargs: (None, old),
    )
    monkeypatch.setattr(
        resume_replace_service, "get_resume_candidate_ttl_days", lambda _db: 7,
    )
    attach_media = MagicMock(return_value=["media/resume-candidate.jpg"])
    monkeypatch.setattr(resume_replace_service, "attach_media", attach_media)
    _, candidate = resume_replace_service.create_replacement_candidate(
        db,
        owner_userid="worker-1",
        target_resume_id=old.id,
        expected_version=4,
        operation_id="00000000-0000-0000-0000-000000000100",
        source_msg_id="stage4-media-binding",
        complete_data={
            "expected_cities": ["苏州"],
            "expected_job_categories": ["普工"],
            "salary_expect_floor_monthly": 6000,
            "gender": "男",
            "age": 31,
        },
        raw_text="带媒体的新简历",
        media_ids=[17],
        audit_result=type("Audit", (), {"status": "pending", "reason": ""})(),
    )

    assert candidate.images == ["media/resume-candidate.jpg"]
    attach_media.assert_called_once_with(
        db, [17], "resume", candidate.id,
        owner_userid="worker-1", entity_version=1,
    )


def test_unit_a_retry_reuses_review_clock_for_activation(db, monkeypatch):
    relation, old, candidate = _graph(db)
    relation.lifecycle_status = "conflict"
    retried_at = datetime(2026, 8, 18, 9, 10, 11, 121314)
    monkeypatch.setattr(resume_replace_service, "utc_now_naive", lambda: retried_at)
    monkeypatch.setattr(
        resume_replace_service,
        "lock_replacement_graph",
        lambda *_args, **_kwargs: (
            relation, [old.id, candidate.id], {old.id: old, candidate.id: candidate},
        ),
    )
    monkeypatch.setattr(resume_replace_service, "write_admin_log", lambda *a, **k: None)
    activation_calls = []

    def record_activation(
        _db, locked_relation, previous, new, *, expected_old_version, now,
    ):
        activation_calls.append(
            (locked_relation, previous, new, expected_old_version, now),
        )
        return True

    monkeypatch.setattr(
        resume_replace_service, "activate_replacement_locked", record_activation,
    )

    assert resume_replace_service.retry_activation(
        db, relation.id, old.version,
        operator="operator-8", reason="conflict resolved",
    ) is True
    assert relation.reviewed_by == "operator-8"
    assert relation.reviewed_at == retried_at
    assert activation_calls == [(relation, old, candidate, old.version, retried_at)]
