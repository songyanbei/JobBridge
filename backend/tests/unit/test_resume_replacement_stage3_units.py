"""Stage 3 direct tests, one small executable contract per A/B/C unit."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from app.llm.base import IntentResult
from app.schemas.conversation import SessionState
from app.services import command_service, intent_service, upload_service
from app.services.resume_business_digest_service import business_digest
from app.services.user_service import UserContext


def _worker(role="worker"):
    return UserContext(
        external_userid="worker-1", role=role, status="active", display_name=None,
        company=None, contact_person=None, phone=None, can_search_jobs=True,
        can_search_workers=False, is_first_touch=False, should_welcome=False,
    )


def test_unit_a_exact_aliases_and_numeric_argument():
    for text in ("/更新简历", "更新简历", "修改简历", "重新提交简历"):
        result = intent_service.classify_intent(text, "worker")
        assert result.structured_data == {"command": "update_resume"}
    result = intent_service.classify_intent("/更新简历 123", "worker")
    assert result.structured_data == {"command": "update_resume", "args": "123"}


def test_unit_a_disabled_and_non_worker_create_no_assignment(monkeypatch):
    monkeypatch.setattr("app.config.settings.resume_replacement_enabled", False)
    db = MagicMock()
    command_service.execute("update_resume", "", _worker(), SessionState(role="worker"), db)
    db.query.assert_not_called()
    monkeypatch.setattr("app.config.settings.resume_replacement_enabled", True)
    command_service.execute("update_resume", "", _worker("factory"), SessionState(role="factory"), db)
    db.query.assert_not_called()


def test_unit_a_database_allowlist_freezes_revision_and_cohort():
    from app.services.resume_rollout_service import assign_operation
    config_query = MagicMock()
    config_query.filter.return_value.with_for_update.return_value.one.return_value = SimpleNamespace(
        config_value='{"revision":12,"userids":["worker-1"]}',
    )
    first_lookup = MagicMock()
    first_lookup.filter.return_value.first.return_value = None
    second_lookup = MagicMock()
    second_lookup.filter.return_value.first.return_value = None
    db = MagicMock()
    db.query.side_effect = [first_lookup, config_query, second_lookup]
    row = assign_operation(
        db, operation_id="operation-1", source_msg_id="message-1", owner_userid="worker-1",
    )
    assert row.cohort == "enabled"
    assert row.allowlist_revision == 12
    db.add.assert_called_once_with(row)


def test_unit_a_existing_assignment_does_not_re_read_changed_allowlist():
    from app.services.resume_rollout_service import assign_operation
    existing = SimpleNamespace(owner_userid="worker-1", cohort="control", allowlist_revision=3)
    lookup = MagicMock()
    lookup.filter.return_value.first.return_value = existing
    db = MagicMock()
    db.query.return_value = lookup
    assert assign_operation(
        db, operation_id="operation-1", source_msg_id="message-1", owner_userid="worker-1",
    ) is existing
    assert db.query.call_count == 1


def test_unit_a_control_cohort_never_reads_or_selects_a_resume(monkeypatch):
    monkeypatch.setattr("app.config.settings.resume_replacement_enabled", True)
    monkeypatch.setattr(
        "app.services.resume_rollout_service.assign_operation",
        lambda *a, **k: SimpleNamespace(
            operation_id="operation-control", cohort="control", allowlist_revision=8,
        ),
    )
    db = MagicMock()
    replies = command_service.execute(
        "update_resume", "", _worker(), SessionState(role="worker"), db,
        source_msg_id="message-control",
    )
    db.query.assert_not_called()
    assert "暂未对您的账号开放" in replies[0].content


def test_unit_a_multiple_online_resumes_requires_explicit_owned_id(monkeypatch):
    monkeypatch.setattr("app.config.settings.resume_replacement_enabled", True)
    monkeypatch.setattr(
        "app.services.resume_rollout_service.assign_operation",
        lambda *a, **k: SimpleNamespace(
            operation_id="operation-1", cohort="enabled", allowlist_revision=8,
        ),
    )
    rows = [
        SimpleNamespace(
            id=11, expected_cities=["苏州"], expected_job_categories=["电子厂"],
            expires_at=datetime.utcnow() + timedelta(days=10), version=1,
        ),
        SimpleNamespace(
            id=12, expected_cities=["昆山"], expected_job_categories=["物流"],
            expires_at=datetime.utcnow() + timedelta(days=10), version=1,
        ),
    ]
    resume_query = MagicMock()
    resume_query.filter.return_value.order_by.return_value.all.return_value = rows
    relation_query = MagicMock()
    relation_query.filter.return_value.all.return_value = []
    db = MagicMock()
    db.query.side_effect = [resume_query, relation_query]

    replies = command_service.execute(
        "update_resume", "", _worker(), SessionState(role="worker"), db,
        source_msg_id="message-1",
    )
    assert "/更新简历 ID" in replies[0].content
    assert "11（" in replies[0].content and "12（" in replies[0].content


def test_unit_b_blank_draft_and_resume_media_are_isolated(monkeypatch):
    monkeypatch.setattr("app.config.settings.resume_replacement_enabled", True)
    assignment = SimpleNamespace(
        operation_id="op-1", cohort="enabled", allowlist_revision=7,
    )
    monkeypatch.setattr("app.services.resume_rollout_service.assign_operation", lambda *a, **k: assignment)
    old = SimpleNamespace(
        id=9, version=3, expected_cities=["旧城市"], expected_job_categories=["旧工种"],
        expires_at=datetime.utcnow() + timedelta(days=5),
    )
    resume_query = MagicMock()
    resume_query.filter.return_value.order_by.return_value.all.return_value = [old]
    relation_query = MagicMock()
    relation_query.filter.return_value.all.return_value = []
    db = MagicMock()
    db.query.side_effect = [resume_query, relation_query]
    session = SessionState(
        role="worker", search_criteria={"city": ["旧搜索"]},
        pending_upload={"education": "旧值"},
    )
    command_service.execute(
        "update_resume", "9", _worker(), session, db, source_msg_id="msg-1",
    )
    assert session.pending_upload == {}
    assert session.search_criteria == {}
    assert session.pending_upload_intent == "upload_resume"
    assert session.pending_operation_id == "op-1"
    assert upload_service.attach_image("worker-1", "key", session, MagicMock(), 42) == "图片已加入新简历草稿（第 1 张）。"


def test_unit_b_digest_is_versioned_and_candidate_does_not_inherit_optional_fields():
    from app.services.resume_replace_service import _candidate
    audit = SimpleNamespace(status="pending", reason="")
    candidate = _candidate(
        "worker-1",
        {"expected_cities": ["新城市"], "expected_job_categories": ["新工种"],
         "salary_expect_floor_monthly": 5000, "gender": "女", "age": 28},
        "本次完整提交", audit, datetime.utcnow() + timedelta(days=7),
    )
    assert candidate.education is None
    assert candidate.work_experience is None
    assert candidate.images is None
    assert len(business_digest(candidate)) == 64
    with pytest.raises(ValueError, match="unsupported resume digest version"):
        business_digest(candidate, digest_version=2)


def test_unit_b_digest_changes_when_only_raw_text_changes():
    first = SimpleNamespace(raw_text="本次完整提交 A")
    second = SimpleNamespace(raw_text="本次完整提交 B")
    assert business_digest(first) != business_digest(second)


def test_unit_b_digest_normalizes_raw_text_newlines_and_unicode():
    decomposed = SimpleNamespace(raw_text="Cafe\u0301\r\n第二行\r第三行")
    normalized = SimpleNamespace(raw_text="Caf\u00e9\n第二行\n第三行")
    assert business_digest(decomposed) == business_digest(normalized)


def test_unit_b_cancel_or_timeout_marks_only_pending_draft_media_for_deletion(monkeypatch):
    mark_delete_pending = MagicMock()
    monkeypatch.setattr(
        "app.services.job_media_service.mark_delete_pending", mark_delete_pending,
    )
    session = SessionState(
        role="worker", pending_upload_intent="upload_resume",
        pending_upload_mode="replace", pending_target_id=10,
        pending_operation_id="operation-1", pending_upload_media_ids=[31, 32],
    )
    db = MagicMock()
    upload_service.abandon_pending_upload(session, db)
    mark_delete_pending.assert_called_once_with(db, [31, 32])
    db.flush.assert_called_once_with()
    assert session.pending_upload_media_ids == []
    assert session.pending_target_id is None
    assert session.pending_operation_id is None


def test_unit_b_complete_resume_draft_routes_only_new_payload_and_media(monkeypatch):
    from app.services import resume_replace_service

    monkeypatch.setattr(upload_service, "_read_ttl_days", lambda *a: 30)
    monkeypatch.setattr(
        upload_service.audit_service, "audit_content_only",
        lambda **k: SimpleNamespace(status="pending", reason="manual review"),
    )
    monkeypatch.setattr(
        upload_service.audit_service, "write_audit_log_for_result", lambda *a, **k: None,
    )
    created = SimpleNamespace(id=22, audit_status="pending", expires_at=None, deleted_at=None)
    create = MagicMock(return_value=(SimpleNamespace(id=21), created))
    monkeypatch.setattr(resume_replace_service, "create_replacement_candidate", create)
    session = SessionState(
        role="worker", pending_upload_intent="upload_resume",
        pending_upload_mode="replace", pending_target_id=10,
        pending_target_version=4, pending_operation_id="operation-1",
        pending_rollout_cohort="enabled", pending_rollout_revision=9,
        pending_upload_media_ids=[31, 32], pending_upload={"education": "旧学历"},
    )
    payload = {
        "expected_cities": ["昆山市"], "expected_job_categories": ["物流仓储"],
        "salary_expect_floor_monthly": 6000, "gender": "男", "age": 30,
    }
    result = upload_service.process_upload(
        _worker(), IntentResult(intent="upload_resume", structured_data=payload),
        "完整新简历", [], session, MagicMock(), source_msg_id="message-1",
    )
    assert result.success is True and result.entity_id == 22
    kwargs = create.call_args.kwargs
    assert kwargs["target_resume_id"] == 10
    assert kwargs["operation_id"] == "operation-1"
    assert kwargs["source_msg_id"] == "message-1"
    assert kwargs["complete_data"] == payload
    assert "education" not in kwargs["complete_data"]
    assert kwargs["media_ids"] == [31, 32]


def test_unit_c_candidate_creation_uses_new_id_and_single_active_relation(monkeypatch):
    from app.services import resume_replace_service as service
    old = SimpleNamespace(
        id=10, owner_userid="worker-1", audit_status="passed",
        activated_at=datetime.utcnow() - timedelta(days=1),
        expires_at=datetime.utcnow() + timedelta(days=20), deleted_at=None,
        delist_reason=None, version=4,
    )
    monkeypatch.setattr(service, "lock_replacement_creation", lambda *a, **k: (None, old))
    monkeypatch.setattr(service, "get_resume_candidate_ttl_days", lambda db: 7)
    monkeypatch.setattr("app.config.settings.resume_replacement_enabled", True)
    db = MagicMock()
    added = []
    def add(row):
        added.append(row)
        if row.__class__.__name__ == "Resume":
            row.id = 11
        else:
            row.id = 12
    db.add.side_effect = add
    relation, candidate = service.create_replacement_candidate(
        db, owner_userid="worker-1", target_resume_id=10, expected_version=4,
        operation_id="operation-1", source_msg_id="message-1",
        complete_data={"expected_cities": ["苏州市"], "expected_job_categories": ["电子厂"],
                       "salary_expect_floor_monthly": 5500, "gender": "男", "age": 30},
        raw_text="完整新简历", media_ids=[],
        audit_result=SimpleNamespace(status="pending", reason=""),
    )
    assert candidate.id == 11 and candidate.activated_at is None and candidate.expires_at is None
    assert relation.active_old_resume_id == 10
    assert relation.old_resume_version == 5
    assert relation.new_resume_id == 11


def test_unit_c_operation_or_message_replay_returns_same_candidate(monkeypatch):
    from app.services import resume_replace_service as service
    existing = SimpleNamespace(
        operation_id="operation-1", source_msg_id="message-1", owner_userid="worker-1",
        old_resume_id=10, new_resume_id=11,
    )
    candidate = SimpleNamespace(id=11)
    monkeypatch.setattr(service, "lock_replacement_creation", lambda *a, **k: (existing, None))
    monkeypatch.setattr("app.config.settings.resume_replacement_enabled", True)
    query = MagicMock()
    query.filter.return_value.with_for_update.return_value.one_or_none.return_value = candidate
    db = MagicMock()
    db.query.return_value = query
    relation, returned = service.create_replacement_candidate(
        db, owner_userid="worker-1", target_resume_id=10, expected_version=5,
        operation_id="operation-1", source_msg_id="different-retry-message",
        complete_data={}, raw_text="", media_ids=[], audit_result=SimpleNamespace(status="pending", reason=""),
    )
    assert relation is existing and returned is candidate
    db.add.assert_not_called()


def test_unit_c_rollback_discards_created_telemetry():
    from app.services.resume_replace_service import (
        _PENDING_EVENTS, _discard_rolled_back_events,
    )
    transaction = SimpleNamespace(parent=None)
    db = SimpleNamespace(info={_PENDING_EVENTS: [(transaction, {"batch_id": "op"})]})
    _discard_rolled_back_events(db, transaction)
    assert _PENDING_EVENTS not in db.info


def test_unit_c_telemetry_emits_only_after_outer_commit(monkeypatch):
    from app.services import resume_replace_service as service

    emitted = []
    monkeypatch.setattr(
        service, "log_event",
        lambda event_name, **fields: emitted.append((event_name, fields)),
    )
    db = Session()
    try:
        db.begin()
        service._schedule_started(
            db,
            SimpleNamespace(
                old_resume_id=10, operation_id="operation-1", owner_userid="worker-1",
            ),
            SimpleNamespace(id=11),
        )
        assert emitted == []
        db.commit()
        assert emitted[0][0] == "resume_replace_started"
        assert emitted[0][1]["old_resume_id"] == 10
        assert emitted[0][1]["new_resume_id"] == 11
        assert emitted[0][1]["batch_id"] == "operation-1"
        assert emitted[0][1]["user_hash"] != "worker-1"

        db.begin()
        service._schedule_started(
            db,
            SimpleNamespace(
                old_resume_id=12, operation_id="operation-rollback", owner_userid="worker-1",
            ),
            SimpleNamespace(id=13),
        )
        db.rollback()
        db.commit()
        assert len(emitted) == 1
    finally:
        db.close()
