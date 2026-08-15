from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, event
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.core.exceptions import BusinessException
from app.core.logging_setup import identifier_hash
from app.api.admin.jobs import ReplacementCancelRequest
from app.models import (
    AuditLog,
    Job,
    JobReplacement,
    MediaAssetLifecycle,
    SystemConfig,
    TargetCleanupTask,
    User,
)
from app.schemas.conversation import SessionState
from app.services.audit_workbench_service import edit_action, pass_action, reject_action, undo
from app.services.command_service import execute
from app.services.job_replace_service import (
    activate_replacement,
    cancel_candidate,
    create_replacement_candidate,
    retry_activation,
)
from app.services import upload_service
from app.services import job_replace_service
from app.services.intent_service import _match_command
from app.services.user_service import UserContext


@compiles(mysql.TINYINT, "sqlite")
@compiles(mysql.SMALLINT, "sqlite")
@compiles(mysql.INTEGER, "sqlite")
@compiles(mysql.BIGINT, "sqlite")
def _compile_mysql_integer_for_sqlite(_type, _compiler, **_kwargs):
    return "INTEGER"


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    tables = (
        User.__table__,
        Job.__table__,
        JobReplacement.__table__,
        MediaAssetLifecycle.__table__,
        TargetCleanupTask.__table__,
        SystemConfig.__table__,
        AuditLog.__table__,
    )
    for table in tables:
        table.create(engine)
    session = sessionmaker(bind=engine)()
    session.add(User(external_userid="owner-1", role="factory"))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _enable_replacement_feature(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "job_replacement_enabled", True)


def _job(db, **overrides):
    values = {
        "owner_userid": "owner-1",
        "city": "苏州",
        "job_category": "普工",
        "salary_floor_monthly": 5000,
        "pay_type": "月薪",
        "headcount": 10,
        "gender_required": "不限",
        "is_long_term": True,
        "raw_text": "旧岗位",
        "description": "旧岗位说明",
        "audit_status": "passed",
        "activated_at": datetime.now() - timedelta(days=1),
        "expires_at": datetime.now() + timedelta(days=30),
        "version": 1,
    }
    values.update(overrides)
    row = Job(**values)
    db.add(row)
    db.commit()
    return row


def _complete_data(**overrides):
    data = {
        "city": "无锡",
        "job_category": "操作工",
        "salary_floor_monthly": 6500,
        "pay_type": "月薪",
        "headcount": 20,
    }
    data.update(overrides)
    return data


def _audit(status):
    return SimpleNamespace(status=status, reason="待人工复核" if status == "pending" else "")


def _create(db, old, status="pending", operation_id="op-1", source_msg_id="msg-1", media_ids=None):
    return create_replacement_candidate(
        db,
        owner_userid="owner-1",
        target_job_id=old.id,
        expected_version=old.version,
        operation_id=operation_id,
        source_msg_id=source_msg_id,
        complete_data=_complete_data(),
        raw_text="完整的新岗位",
        media_ids=media_ids or [],
        audit_result=_audit(status),
    )


def test_replacement_candidate_preserves_job_visibility_fields(db):
    old = _job(db)
    _, candidate = create_replacement_candidate(
        db,
        owner_userid="owner-1",
        target_job_id=old.id,
        expected_version=old.version,
        operation_id="op-visibility-fields",
        source_msg_id="msg-visibility-fields",
        complete_data=_complete_data(
            hiring_company="华星电子",
            address="木渎镇金山路88号",
            contact_person="张经理",
            phone="13800138000",
        ),
        raw_text="完整的新岗位",
        media_ids=[],
        audit_result=_audit("pending"),
    )

    assert candidate.hiring_company == "华星电子"
    assert candidate.address == "木渎镇金山路88号"
    assert candidate.contact_person == "张经理"
    assert candidate.phone == "13800138000"


def test_update_command_selects_only_job_and_starts_empty_draft(db, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "job_replacement_enabled", True)
    old = _job(db)
    session = SessionState(
        role="factory",
        pending_upload={"city": "旧草稿"},
        search_criteria={"city": ["苏州市"], "job_category": ["电子厂"]},
        last_criteria={"city": ["苏州市"]},
        shown_items=["99"],
        history=[{"role": "user", "content": "帮我找工人"}],
        pending_relaxation={"direction": "search_worker"},
    )
    user = UserContext(
        external_userid="owner-1", role="factory", status="active",
        display_name=None, company=None, contact_person=None, phone=None,
        can_search_jobs=False, can_search_workers=True,
        is_first_touch=False, should_welcome=False,
    )

    reply = execute("update_job", str(old.id), user, session, db)[0]

    assert "完整的新岗位信息" in reply.content
    assert session.pending_upload == {}
    assert session.search_criteria == {}
    assert session.last_criteria == {}
    assert session.shown_items == []
    assert session.history == []
    assert session.pending_relaxation is None
    assert session.pending_upload_mode == "replace"
    assert session.pending_target_id == old.id
    started_at = datetime.fromisoformat(session.pending_started_at)
    updated_at = datetime.fromisoformat(session.pending_updated_at)
    expires_at = datetime.fromisoformat(session.pending_expires_at)
    assert updated_at == started_at
    assert expires_at - started_at == timedelta(minutes=10)

    original_deadline = session.pending_expires_at
    upload_service._save_pending_upload(
        session,
        intent="upload_job",
        structured_data={"city": "无锡"},
        missing=["job_category"],
        raw_text="无锡",
    )
    assert session.pending_expires_at == original_deadline


@pytest.mark.parametrize("text", [
    "/更新岗位", "更新岗位", "修改岗位", "重新发布岗位",
])
def test_update_job_exact_aliases(text):
    assert _match_command(text) == ("update_job", "")


def test_update_job_command_parses_explicit_id():
    assert _match_command("/更新岗位 123") == ("update_job", "123")


def test_update_command_requires_explicit_id_for_multiple_jobs(db, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "job_replacement_enabled", True)
    first = _job(db)
    second = _job(db)
    session = SessionState(role="factory")
    user = UserContext(
        external_userid="owner-1", role="factory", status="active",
        display_name=None, company=None, contact_person=None, phone=None,
        can_search_jobs=False, can_search_workers=True,
        is_first_touch=False, should_welcome=False,
    )

    reply = execute("update_job", "", user, session, db)[0]

    assert str(first.id) in reply.content and str(second.id) in reply.content
    assert session.pending_target_id is None


def test_pending_candidate_is_complete_and_does_not_replace_old_job(db):
    old = _job(db, address="旧地址")
    old_version = old.version
    relation, candidate = _create(db, old, status="pending")
    db.commit()

    assert candidate.address is None
    assert candidate.city == "无锡"
    assert candidate.audit_status == "pending"
    assert candidate.expires_at is None
    assert candidate.candidate_expires_at is not None
    assert old.deleted_at is None and old.delist_reason is None
    assert old.version == old_version + 1
    assert relation.old_job_version == old.version
    assert relation.lifecycle_status == "awaiting_review"
    assert relation.active_old_job_id == old.id


def test_pending_candidate_creation_emits_one_privacy_safe_event(db, monkeypatch):
    events = []
    monkeypatch.setattr(
        job_replace_service,
        "log_event",
        lambda event, **fields: events.append((event, fields)),
    )
    old = _job(db)

    relation, candidate = _create(db, old, status="pending")
    assert events == []
    db.commit()
    duplicate_relation, duplicate_candidate = _create(
        db,
        old,
        operation_id="op-1",
        source_msg_id="msg-replayed",
    )
    relation_by_message, candidate_by_message = _create(
        db,
        old,
        operation_id="op-replayed",
        source_msg_id="msg-1",
    )

    assert duplicate_relation.id == relation.id
    assert duplicate_candidate.id == candidate.id
    assert relation_by_message.id == relation.id
    assert candidate_by_message.id == candidate.id
    assert events == [
        (
            "job_replace_started",
            {
                "old_job_id": old.id,
                "new_job_id": candidate.id,
                "batch_id": "op-1",
                "user_hash": identifier_hash("owner-1"),
            },
        )
    ]
    assert events[0][1]["user_hash"] != "owner-1"
    assert set(events[0][1]) == {
        "old_job_id",
        "new_job_id",
        "batch_id",
        "user_hash",
    }


def test_candidate_creation_event_is_discarded_on_rollback(db, monkeypatch):
    events = []
    monkeypatch.setattr(
        job_replace_service,
        "log_event",
        lambda event_name, **fields: events.append((event_name, fields)),
    )
    old = _job(db)

    _create(db, old, status="pending")
    db.rollback()
    db.commit()

    assert events == []
    assert db.query(JobReplacement).count() == 0


def test_candidate_creation_event_is_not_emitted_on_commit_failure(db, monkeypatch):
    events = []
    monkeypatch.setattr(
        job_replace_service,
        "log_event",
        lambda event_name, **fields: events.append((event_name, fields)),
    )
    old = _job(db)
    _create(db, old, status="pending")

    def fail_commit(_db):
        raise RuntimeError("forced_commit_failure")

    event.listen(db, "before_commit", fail_commit, once=True)
    with pytest.raises(RuntimeError, match="forced_commit_failure"):
        db.commit()
    assert events == []
    db.rollback()
    db.commit()

    assert events == []
    assert db.query(JobReplacement).count() == 0


def test_candidate_creation_event_waits_for_outer_commit(db, monkeypatch):
    events = []
    monkeypatch.setattr(
        job_replace_service,
        "log_event",
        lambda event_name, **fields: events.append((event_name, fields)),
    )
    old = _job(db)

    savepoint = db.begin_nested()
    _create(db, old, status="pending")
    savepoint.commit()
    assert events == []

    db.commit()
    assert len(events) == 1
    assert events[0][0] == "job_replace_started"


def test_savepoint_rollback_discards_only_nested_candidate_event(db, monkeypatch):
    events = []
    monkeypatch.setattr(
        job_replace_service,
        "log_event",
        lambda event_name, **fields: events.append((event_name, fields)),
    )
    outer_old = _job(db)
    nested_old = _job(db)

    outer_relation, _ = _create(
        db,
        outer_old,
        operation_id="op-outer",
        source_msg_id="msg-outer",
    )
    savepoint = db.begin_nested()
    _create(
        db,
        nested_old,
        operation_id="op-nested",
        source_msg_id="msg-nested",
    )
    savepoint.rollback()
    assert events == []

    db.commit()

    assert db.query(JobReplacement).count() == 1
    assert db.query(JobReplacement).one().id == outer_relation.id
    assert len(events) == 1
    assert events[0][1]["batch_id"] == "op-outer"


def test_telemetry_failure_does_not_fail_commit_or_leak_event(db, monkeypatch):
    attempts = []

    def fail_telemetry(event_name, **fields):
        attempts.append((event_name, fields))
        raise RuntimeError("telemetry_unavailable")

    monkeypatch.setattr(job_replace_service, "log_event", fail_telemetry)
    old = _job(db)
    relation, candidate = _create(db, old, status="pending")

    db.commit()

    assert relation.id is not None and candidate.id is not None
    assert db.query(JobReplacement).filter_by(id=relation.id).one()
    assert len(attempts) == 1
    assert job_replace_service._PENDING_REPLACEMENT_EVENTS not in db.info
    db.commit()
    assert len(attempts) == 1


def test_candidate_creation_event_is_discarded_when_session_closes(db, monkeypatch):
    events = []
    monkeypatch.setattr(
        job_replace_service,
        "log_event",
        lambda event_name, **fields: events.append((event_name, fields)),
    )
    old = _job(db)

    _create(db, old, status="pending")
    db.close()
    db.commit()

    assert events == []
    assert db.query(JobReplacement).count() == 0


def test_replacement_candidate_preserves_explicit_full_update_fields(db):
    old = _job(db)
    relation, candidate = create_replacement_candidate(
        db,
        owner_userid="owner-1",
        target_job_id=old.id,
        expected_version=old.version,
        operation_id="op-full-fields",
        source_msg_id="msg-full-fields",
        complete_data=_complete_data(
            address="星湖街88号",
            accept_couple=True,
            employment_type="厂家直招",
            contract_type="长期合同",
        ),
        raw_text="完整的新岗位",
        media_ids=[],
        audit_result=_audit("pending"),
    )
    db.commit()

    assert relation.old_job_id == old.id
    assert candidate.address == "星湖街88号"
    assert bool(candidate.accept_couple) is True
    assert candidate.employment_type == "厂家直招"
    assert candidate.contract_type == "长期合同"


def test_candidate_creation_is_blocked_by_rollout_gate(db, monkeypatch):
    from app.config import settings

    old = _job(db)
    old_version = old.version
    monkeypatch.setattr(settings, "job_replacement_enabled", False)

    with pytest.raises(BusinessException, match="job_replacement_disabled"):
        _create(db, old)

    assert old.version == old_version
    assert db.query(JobReplacement).count() == 0
    assert db.query(Job).count() == 1


def test_rejected_candidate_closes_relation_and_keeps_old_job_online(db):
    old = _job(db)
    relation, candidate = _create(db, old, status="rejected")
    db.commit()

    assert candidate.audit_status == "rejected"
    assert relation.review_outcome == "rejected"
    assert relation.lifecycle_status == "closed"
    assert old.deleted_at is None and old.delist_reason is None


def test_auto_pass_atomically_activates_new_job_and_replaces_old(db):
    old = _job(db)
    old_version = old.version
    relation, candidate = _create(db, old, status="passed")
    db.commit()

    assert candidate.audit_status == "passed"
    assert candidate.activated_at is not None and candidate.expires_at is not None
    assert candidate.candidate_expires_at is None
    assert old.delist_reason == "replaced" and old.deleted_at is not None
    assert relation.old_job_version == old_version + 1
    assert old.version == old_version + 2
    assert relation.lifecycle_status == "activated"
    assert db.query(TargetCleanupTask).filter_by(target_id=old.id).one().reason == "replaced"


def test_manual_pass_activates_first_publish_candidate(db):
    candidate = _job(
        db,
        audit_status="pending",
        activated_at=None,
        expires_at=None,
        candidate_expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=7),
    )

    pass_action(db, "job", candidate.id, candidate.version, "reviewer")

    assert candidate.audit_status == "passed"
    assert candidate.activated_at is not None and candidate.expires_at is not None
    assert candidate.candidate_expires_at is None


def test_duplicate_operation_or_source_message_returns_same_candidate(db):
    old = _job(db)
    first_relation, first_candidate = _create(db, old)
    db.commit()

    relation_by_operation, candidate_by_operation = _create(
        db, old, operation_id="op-1", source_msg_id="msg-2",
    )
    relation_by_message, candidate_by_message = _create(
        db, old, operation_id="op-2", source_msg_id="msg-1",
    )

    assert relation_by_operation.id == first_relation.id
    assert relation_by_message.id == first_relation.id
    assert candidate_by_operation.id == first_candidate.id
    assert candidate_by_message.id == first_candidate.id


def test_manual_pass_conflict_keeps_candidate_pending_then_retry_activates(db):
    old = _job(db)
    relation, candidate = _create(db, old)
    db.commit()
    old.description = "审核期间被管理员修改"
    old.version += 1
    db.commit()

    pass_action(db, "job", candidate.id, candidate.version, "reviewer")
    db.refresh(relation)
    db.refresh(candidate)
    assert relation.review_outcome == "passed"
    assert relation.lifecycle_status == "conflict"
    assert candidate.audit_status == "pending" and candidate.expires_at is None

    assert retry_activation(
        db, relation.id, old.version, operator="reviewer", reason="确认使用当前旧版本",
    )
    db.commit()
    assert candidate.audit_status == "passed"
    assert relation.lifecycle_status == "activated"


def test_reviewed_conflict_candidate_cannot_be_edited_before_retry(db):
    old = _job(db)
    relation, candidate = _create(db, old)
    db.commit()
    old.description = "审核期间被管理员修改"
    old.version += 1
    db.commit()

    pass_action(db, "job", candidate.id, candidate.version, "reviewer")
    original_description = candidate.description
    original_version = candidate.version

    with pytest.raises(BusinessException, match="replacement_already_reviewed"):
        edit_action(
            db,
            "job",
            candidate.id,
            candidate.version,
            {"description": "试图绕过重新审核"},
            "reviewer",
        )
    db.rollback()
    db.refresh(candidate)
    db.refresh(relation)

    assert candidate.description == original_description
    assert candidate.version == original_version
    assert relation.review_outcome == "passed"
    assert relation.lifecycle_status == "conflict"


def test_pending_replacement_candidate_edit_stays_awaiting_review(db, monkeypatch):
    old = _job(db)
    relation, candidate = _create(db, old)
    db.commit()
    previous_version = candidate.version
    monkeypatch.setattr(
        "app.services.audit_workbench_service.save_undo",
        lambda *_args, **_kwargs: None,
    )

    edit_action(
        db,
        "job",
        candidate.id,
        candidate.version,
        {"description": "审核员修正后的完整内容"},
        "reviewer",
    )

    assert candidate.description == "审核员修正后的完整内容"
    assert candidate.version == previous_version + 1
    assert relation.review_outcome == "pending"
    assert relation.lifecycle_status == "awaiting_review"


def test_manual_reject_and_operator_cancel_preserve_old_and_release_media(db):
    old = _job(db)
    media = MediaAssetLifecycle(
        object_key="jobs/draft.jpg", owner_userid="owner-1", state="pending",
    )
    db.add(media)
    db.commit()
    relation, candidate = _create(db, old, media_ids=[media.id])
    db.commit()

    reject_action(db, "job", candidate.id, candidate.version, "不合规", "reviewer")
    assert relation.lifecycle_status == "closed"
    assert relation.review_outcome == "rejected"
    assert old.deleted_at is None
    assert media.entity_id == candidate.id and media.entity_id != old.id

    other_relation, other_candidate = _create(
        db, old, operation_id="op-2", source_msg_id="msg-2",
    )
    db.commit()
    cancel_candidate(db, other_relation.id, operator="reviewer")
    db.commit()
    assert other_relation.lifecycle_status == "closed"
    assert other_candidate.deleted_at is not None


def test_replacement_cancel_reason_is_limited_at_api_and_service_boundaries(db):
    accepted_reason = "r" * 64
    rejected_reason = "r" * 65

    assert ReplacementCancelRequest(reason=accepted_reason).reason == accepted_reason
    with pytest.raises(ValidationError):
        ReplacementCancelRequest(reason=rejected_reason)

    old = _job(db)
    relation, candidate = _create(db, old)
    db.commit()

    with pytest.raises(BusinessException, match="replacement_cancel_reason_invalid"):
        cancel_candidate(
            db,
            relation.id,
            operator="reviewer",
            reason=rejected_reason,
        )
    assert relation.lifecycle_status == "awaiting_review"
    assert relation.closed_reason is None
    assert candidate.deleted_at is None

    cancel_candidate(
        db,
        relation.id,
        operator="reviewer",
        reason=accepted_reason,
    )
    db.flush()
    assert relation.closed_reason == accepted_reason


def test_retry_rejects_expired_candidate(db):
    old = _job(db)
    relation, candidate = _create(db, old)
    relation.review_outcome = "passed"
    relation.lifecycle_status = "conflict"
    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    candidate.candidate_expires_at = utc_now - timedelta(seconds=1)
    db.commit()
    db.expire_all()
    assert db.get(Job, candidate.id).candidate_expires_at < utc_now
    from app.services.job_replacement_lock_service import lock_replacement_graph
    locked_relation, _, jobs = lock_replacement_graph(db, relation.id)
    assert jobs[locked_relation.new_job_id].candidate_expires_at < utc_now

    with pytest.raises(BusinessException, match="candidate_expired"):
        retry_activation(db, relation.id, old.version, operator="reviewer", reason="retry")


def test_candidate_rejects_media_owned_by_another_user(db):
    old = _job(db)
    media = MediaAssetLifecycle(
        object_key="jobs/not-owned.jpg", owner_userid="another-user", state="pending",
    )
    db.add(media)
    db.commit()

    with pytest.raises(ValueError, match="media_lifecycle_owner_mismatch"):
        _create(db, old, media_ids=[media.id])


def test_undo_rejects_job_lifecycle_transition_before_consuming_snapshot(db, monkeypatch):
    active = _job(db)
    monkeypatch.setattr(
        "app.services.audit_workbench_service.get_undo",
        lambda *_args: {"action": "pass", "before": {}},
    )
    monkeypatch.setattr(
        "app.services.audit_workbench_service.get_undo_snapshot",
        lambda *_args: ({"action": "pass", "before": {}}, "snapshot"),
    )
    consume = SimpleNamespace(called=False)

    def _consume(*_args):
        consume.called = True
        return "consumed"

    monkeypatch.setattr(
        "app.services.audit_workbench_service.consume_undo_if_unchanged", _consume,
    )

    with pytest.raises(BusinessException, match="job_lifecycle_transition_not_undoable"):
        undo(db, "job", active.id, "reviewer")
    assert consume.called is False


def test_undo_rejects_old_job_after_outgoing_replacement_activated(db, monkeypatch):
    old = _job(db)
    relation, candidate = _create(db, old, status="pending")
    relation.review_outcome = "passed"
    relation.lifecycle_status = "activated"
    relation.active_old_job_id = None
    candidate.audit_status = "passed"
    db.commit()
    payload = {
        "action": "edit",
        "before": {"description": "before", "version": old.version},
        "after": {"description": "after", "version": old.version},
    }
    monkeypatch.setattr(
        "app.services.audit_workbench_service.get_undo", lambda *_args: payload,
    )
    monkeypatch.setattr(
        "app.services.audit_workbench_service.get_undo_snapshot",
        lambda *_args: (payload, "snapshot"),
    )
    consume = SimpleNamespace(called=False)

    def _consume(*_args):
        consume.called = True
        return "consumed"

    monkeypatch.setattr(
        "app.services.audit_workbench_service.consume_undo_if_unchanged", _consume,
    )

    with pytest.raises(BusinessException, match="job_lifecycle_transition_not_undoable"):
        undo(db, "job", old.id, "reviewer")
    assert consume.called is False


def test_undo_does_not_restore_when_validated_snapshot_was_replaced(db, monkeypatch):
    job = _job(db)
    job.description = "current value"
    db.commit()
    payload = {
        "action": "edit",
        "before": {"description": "old value", "version": job.version},
        "after": {"description": "current value", "version": job.version},
    }
    monkeypatch.setattr(
        "app.services.audit_workbench_service.get_undo", lambda *_args: payload,
    )
    monkeypatch.setattr(
        "app.services.audit_workbench_service.get_undo_snapshot",
        lambda *_args: (payload, "validated-snapshot"),
    )
    monkeypatch.setattr(
        "app.services.audit_workbench_service.validate_undo_unchanged",
        lambda *_args: "changed",
    )

    with pytest.raises(BusinessException, match="撤销快照已变化"):
        undo(db, "job", job.id, "reviewer")
    assert job.description == "current value"


def test_undo_commit_failure_preserves_snapshot(db, monkeypatch):
    job = _job(db, description="current value")
    db.commit()
    payload = {
        "action": "edit",
        "before": {"description": "old value", "version": job.version},
        "after": {"description": "current value", "version": job.version},
    }
    consume = SimpleNamespace(called=False)

    monkeypatch.setattr(
        "app.services.audit_workbench_service.get_undo", lambda *_args: payload,
    )
    monkeypatch.setattr(
        "app.services.audit_workbench_service.get_undo_snapshot",
        lambda *_args: (payload, "snapshot-token"),
    )
    monkeypatch.setattr(
        "app.services.audit_workbench_service.validate_undo_unchanged",
        lambda *_args: "unchanged",
    )

    def _consume(*_args):
        consume.called = True
        return "consumed"

    monkeypatch.setattr(
        "app.services.audit_workbench_service.consume_undo_if_unchanged", _consume,
    )

    def _fail_commit(_session):
        raise RuntimeError("forced_undo_commit_failure")

    event.listen(db, "before_commit", _fail_commit, once=True)
    with pytest.raises(RuntimeError, match="forced_undo_commit_failure"):
        undo(db, "job", job.id, "reviewer")
    assert consume.called is False

    db.rollback()
    db.expire_all()
    persisted = db.get(Job, job.id)
    assert persisted.description == "current value"
    assert persisted.version == payload["after"]["version"]


def test_undo_consumes_snapshot_only_after_commit(db, monkeypatch):
    job = _job(db, description="current value")
    db.commit()
    payload = {
        "action": "edit",
        "before": {"description": "old value", "version": job.version},
        "after": {"description": "current value", "version": job.version},
    }
    events = []
    original_commit = db.commit

    monkeypatch.setattr(
        "app.services.audit_workbench_service.get_undo", lambda *_args: payload,
    )
    monkeypatch.setattr(
        "app.services.audit_workbench_service.get_undo_snapshot",
        lambda *_args: (payload, "snapshot-token"),
    )
    monkeypatch.setattr(
        "app.services.audit_workbench_service.validate_undo_unchanged",
        lambda *_args: "unchanged",
    )

    def _commit():
        events.append("commit")
        original_commit()

    def _consume(*_args):
        events.append("consume")
        return "consumed"

    monkeypatch.setattr(db, "commit", _commit)
    monkeypatch.setattr(
        "app.services.audit_workbench_service.consume_undo_if_unchanged", _consume,
    )

    undo(db, "job", job.id, "reviewer")

    assert events == ["commit", "consume"]
    assert job.description == "old value"
    assert job.version == payload["after"]["version"] + 1


def test_undo_commit_success_is_not_reversed_by_snapshot_cleanup_failure(
    db, monkeypatch,
):
    job = _job(db, description="current value")
    db.commit()
    payload = {
        "action": "edit",
        "before": {"description": "old value", "version": job.version},
        "after": {"description": "current value", "version": job.version},
    }
    monkeypatch.setattr(
        "app.services.audit_workbench_service.get_undo", lambda *_args: payload,
    )
    monkeypatch.setattr(
        "app.services.audit_workbench_service.get_undo_snapshot",
        lambda *_args: (payload, "snapshot-token"),
    )
    monkeypatch.setattr(
        "app.services.audit_workbench_service.validate_undo_unchanged",
        lambda *_args: "unchanged",
    )

    def _fail_cleanup(*_args):
        raise ConnectionError("redis unavailable after commit")

    monkeypatch.setattr(
        "app.services.audit_workbench_service.consume_undo_if_unchanged",
        _fail_cleanup,
    )

    undo(db, "job", job.id, "reviewer")

    db.expire_all()
    persisted = db.get(Job, job.id)
    assert persisted.description == "old value"
    assert persisted.version == payload["after"]["version"] + 1
