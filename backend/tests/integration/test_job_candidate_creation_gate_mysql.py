"""First-publish candidate rollout gate on a real MySQL database."""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import event

from app.config import settings
from app.db import SessionLocal
from app.llm.base import IntentResult
from app.models import Job, User
from app.schemas.conversation import SessionState
from app.services import upload_service
from app.services.audit_service import AuditResult
from app.services.user_service import UserContext


pytestmark = pytest.mark.integration


def _user_context(owner_userid: str) -> UserContext:
    return UserContext(
        external_userid=owner_userid,
        role="factory",
        status="active",
        display_name=None,
        company="rollout gate test",
        contact_person=None,
        phone=None,
        can_search_jobs=False,
        can_search_workers=True,
        is_first_touch=False,
        should_welcome=False,
    )


def _intent() -> IntentResult:
    return IntentResult(
        intent="upload_job",
        structured_data={
            "city": "苏州市",
            "job_category": "电子厂",
            "salary_floor_monthly": 5500,
            "pay_type": "月薪",
            "headcount": 30,
        },
        confidence=0.95,
    )


def test_all_disabled_lifecycle_switches_do_not_persist_pending_candidate(
    monkeypatch,
):
    owner_userid = f"candidate-gate-{uuid4().hex}"
    enabled_owner = f"candidate-enabled-{uuid4().hex}"
    for name in (
        "job_replacement_enabled",
        "job_expiry_cleanup_enabled",
        "job_candidate_cleanup_enabled",
        "job_hard_delete_enabled",
    ):
        monkeypatch.setattr(settings, name, False)
    monkeypatch.setattr(
        upload_service.audit_service,
        "audit_content_only",
        lambda **_kwargs: AuditResult(
            status="pending", reason="manual review", matched_words=[]
        ),
    )

    db = SessionLocal()
    try:
        db.add_all([
            User(external_userid=owner_userid, role="factory"),
            User(external_userid=enabled_owner, role="factory"),
        ])
        db.flush()
        result = upload_service.process_upload(
            _user_context(owner_userid),
            _intent(),
            "苏州电子厂招聘普工",
            [],
            SessionState(role="factory"),
            db,
        )

        assert result.success is False
        assert "暂不可用" in result.reply_text
        assert db.query(Job).filter(Job.owner_userid == owner_userid).count() == 0

        monkeypatch.setattr(settings, "job_replacement_enabled", True)
        enabled = upload_service.process_upload(
            _user_context(enabled_owner),
            _intent(),
            "苏州电子厂招聘普工",
            [],
            SessionState(role="factory"),
            db,
        )
        assert enabled.success is True
        candidate = db.query(Job).filter(Job.owner_userid == enabled_owner).one()
        assert candidate.audit_status == "pending"
        assert candidate.activated_at is None
        assert candidate.expires_at is None
        assert candidate.candidate_expires_at is not None
    finally:
        db.rollback()
        db.close()


@pytest.mark.parametrize("audit_status", ["pending", "rejected"])
def test_first_publish_candidate_event_emits_only_after_mysql_commit(
    monkeypatch, audit_status,
):
    owner_userid = f"candidate-event-{audit_status}-{uuid4().hex}"
    events = []
    monkeypatch.setattr(settings, "job_replacement_enabled", True)
    monkeypatch.setattr(
        upload_service.audit_service,
        "audit_content_only",
        lambda **_kwargs: AuditResult(
            status=audit_status, reason="manual review", matched_words=[]
        ),
    )
    monkeypatch.setattr(
        upload_service,
        "log_event",
        lambda event_type, **fields: events.append((event_type, fields)),
    )

    db = SessionLocal()
    try:
        db.add(User(external_userid=owner_userid, role="factory"))
        db.commit()
        result = upload_service.process_upload(
            _user_context(owner_userid),
            _intent(),
            "苏州电子厂招聘普工",
            [],
            SessionState(role="factory"),
            db,
            source_msg_id="first-publish-message",
        )
        assert result.success is True
        assert events == []

        db.commit()

        assert len(events) == 1
        event_type, fields = events[0]
        assert event_type == "job_candidate_created"
        assert fields == {
            "job_id": result.entity_id,
            "candidate_kind": "first_publish",
            "audit_status": audit_status,
            "source_message_id": "first-publish-message",
            "user_hash": upload_service.identifier_hash(owner_userid),
        }
    finally:
        db.rollback()
        db.query(Job).filter(Job.owner_userid == owner_userid).delete(
            synchronize_session=False,
        )
        db.query(User).filter(User.external_userid == owner_userid).delete(
            synchronize_session=False,
        )
        db.commit()
        db.close()


def test_first_publish_candidate_event_is_discarded_on_commit_failure(monkeypatch):
    owner_userid = f"candidate-event-fail-{uuid4().hex}"
    events = []
    monkeypatch.setattr(settings, "job_replacement_enabled", True)
    monkeypatch.setattr(
        upload_service.audit_service,
        "audit_content_only",
        lambda **_kwargs: AuditResult(
            status="pending", reason="manual review", matched_words=[]
        ),
    )
    monkeypatch.setattr(
        upload_service,
        "log_event",
        lambda event_type, **fields: events.append((event_type, fields)),
    )

    db = SessionLocal()
    try:
        db.add(User(external_userid=owner_userid, role="factory"))
        db.commit()
        result = upload_service.process_upload(
            _user_context(owner_userid),
            _intent(),
            "苏州电子厂招聘普工",
            [],
            SessionState(role="factory"),
            db,
            source_msg_id="failed-first-publish-message",
        )

        def _fail_commit(_session):
            raise RuntimeError("forced_candidate_commit_failure")

        event.listen(db, "before_commit", _fail_commit, once=True)
        with pytest.raises(RuntimeError, match="forced_candidate_commit_failure"):
            db.commit()
        db.rollback()

        assert events == []
        verify = SessionLocal()
        try:
            assert verify.get(Job, result.entity_id) is None
        finally:
            verify.close()
    finally:
        db.rollback()
        db.query(Job).filter(Job.owner_userid == owner_userid).delete(
            synchronize_session=False,
        )
        db.query(User).filter(User.external_userid == owner_userid).delete(
            synchronize_session=False,
        )
        db.commit()
        db.close()


def test_first_publish_event_failure_does_not_invalidate_mysql_commit(monkeypatch):
    owner_userid = f"candidate-event-log-fail-{uuid4().hex}"
    replayed_events = []
    monkeypatch.setattr(settings, "job_replacement_enabled", True)
    monkeypatch.setattr(
        upload_service.audit_service,
        "audit_content_only",
        lambda **_kwargs: AuditResult(
            status="pending", reason="manual review", matched_words=[]
        ),
    )

    def _fail_event(*_args, **_kwargs):
        raise RuntimeError("forced_candidate_event_failure")

    monkeypatch.setattr(upload_service, "log_event", _fail_event)
    db = SessionLocal()
    try:
        db.add(User(external_userid=owner_userid, role="factory"))
        db.commit()
        result = upload_service.process_upload(
            _user_context(owner_userid),
            _intent(),
            "苏州电子厂招聘普工",
            [],
            SessionState(role="factory"),
            db,
            source_msg_id="event-failure-message",
        )

        db.commit()

        verify = SessionLocal()
        try:
            assert verify.get(Job, result.entity_id) is not None
        finally:
            verify.close()
        assert upload_service._PENDING_FIRST_PUBLISH_EVENTS not in db.info

        monkeypatch.setattr(
            upload_service,
            "log_event",
            lambda event_type, **fields: replayed_events.append((event_type, fields)),
        )
        db.commit()
        assert replayed_events == []
    finally:
        db.rollback()
        db.query(Job).filter(Job.owner_userid == owner_userid).delete(
            synchronize_session=False,
        )
        db.query(User).filter(User.external_userid == owner_userid).delete(
            synchronize_session=False,
        )
        db.commit()
        db.close()
