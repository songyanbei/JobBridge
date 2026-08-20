from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.core.exceptions import BusinessException
from app.models import (
    AdminUser, AuditLog, MediaAssetLifecycle, Resume, ResumeMediaIsolationIssue,
    ResumeReplacement, SystemConfig, TargetCleanupTask, User,
)
from app.services.cleanup_admin_service import (
    approve_media_issue, execute_media_issue, list_media_dead_letters, list_media_issues,
    redrive_dead_letters,
)
from app.services.resume_admin_service import list_resumes, replacement_projections
from app.services.resume_replacement_rollout_service import get_allowlist, update_allowlist


@compiles(mysql.TINYINT, "sqlite")
@compiles(mysql.SMALLINT, "sqlite")
@compiles(mysql.INTEGER, "sqlite")
@compiles(mysql.BIGINT, "sqlite")
@compiles(mysql.DATETIME, "sqlite")
def _compile_mysql_for_sqlite(_type, _compiler, **_kwargs):
    return "INTEGER" if not isinstance(_type, mysql.DATETIME) else "DATETIME"


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    for table in (
        User.__table__, Resume.__table__, ResumeReplacement.__table__,
        TargetCleanupTask.__table__, MediaAssetLifecycle.__table__,
        ResumeMediaIsolationIssue.__table__, AuditLog.__table__,
        SystemConfig.__table__, AdminUser.__table__,
    ):
        table.create(engine)
    session = sessionmaker(bind=engine)()
    session.add(User(external_userid="worker-1", role="worker"))
    session.add(AdminUser(
        username="root", password_hash="unused", role="super_admin",
        enabled=1, password_changed=1,
    ))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _resume(db, **overrides):
    values = {
        "owner_userid": "worker-1", "expected_cities": ["苏州"],
        "expected_job_categories": ["普工"], "salary_expect_floor_monthly": 5000,
        "gender": "男", "age": 30, "accept_long_term": 1,
        "accept_short_term": 0, "raw_text": "简历", "audit_status": "passed",
        "activated_at": datetime.utcnow(), "expires_at": datetime.utcnow() + timedelta(days=1),
        "version": 1,
    }
    values.update(overrides)
    row = Resume(**values)
    db.add(row)
    db.flush()
    return row


def test_lifecycle_scopes_are_small_and_default_stays_compatible(db):
    active = _resume(db)
    candidate = _resume(
        db, audit_status="pending", activated_at=None, expires_at=None,
        candidate_expires_at=datetime.utcnow() + timedelta(days=1),
    )
    history = _resume(db, expires_at=datetime.utcnow() - timedelta(seconds=1))
    expired_candidate = _resume(
        db, audit_status="pending", activated_at=None, expires_at=None,
        candidate_expires_at=datetime.utcnow() - timedelta(seconds=1),
    )
    db.commit()

    default, _ = list_resumes(db, {})
    all_rows, _ = list_resumes(db, {}, lifecycle_scope="all")
    scoped = {
        name: {row.id for row in list_resumes(db, {}, lifecycle_scope=name)[0]}
        for name in ("active", "candidate", "history")
    }
    assert {row.id for row in default} == {active.id, candidate.id, history.id, expired_candidate.id}
    assert {row.id for row in all_rows} == {row.id for row in default}
    assert scoped == {
        "active": {active.id}, "candidate": {candidate.id},
        "history": {history.id, expired_candidate.id},
    }
    with pytest.raises(BusinessException, match="lifecycle_scope"):
        list_resumes(db, {}, lifecycle_scope="unknown")


def test_replacement_projection_contains_both_sides_and_conflict(db):
    old, new = _resume(db), _resume(
        db, audit_status="pending", activated_at=None, expires_at=None,
        candidate_expires_at=datetime.utcnow() + timedelta(days=1),
    )
    relation = ResumeReplacement(
        operation_id="00000000-0000-0000-0000-000000000001", source_msg_id="msg-1",
        owner_userid="worker-1", old_resume_id=old.id, new_resume_id=new.id,
        old_resume_version=1, old_business_digest="a" * 64,
        review_outcome="passed", lifecycle_status="conflict",
        conflict_reason="replacement_conflict", active_old_resume_id=old.id,
    )
    db.add(relation)
    db.commit()
    projection = replacement_projections(db, [old, new])
    assert projection[old.id]["replaced_by_resume_id"] == new.id
    assert projection[new.id]["replaces_resume_id"] == old.id
    assert projection[new.id]["replacement_conflict_reason"] == "replacement_conflict"


def test_redrive_is_per_item_and_audit_is_redacted(db):
    target = TargetCleanupTask(
        operation_id="op-1", target_type="resume", target_id=7,
        reason="expired", status="dead_letter", attempt_count=10,
        last_error="secret target error",
    )
    media = MediaAssetLifecycle(
        object_key="private/resume/photo.jpg", owner_userid="worker-1",
        entity_type="resume", entity_id=7, state="dead_letter",
        attempt_count=10, last_error="secret media error",
    )
    db.add_all([target, media])
    db.commit()
    assert redrive_dead_letters(
        db, kind="target", ids=[target.id, 999], reason="manual recovery", operator="root",
    ) == [{"id": target.id, "result": "queued"}, {"id": 999, "result": "not_found"}]
    assert target.status == "pending" and target.last_error is None
    audit = db.query(AuditLog).order_by(AuditLog.id.desc()).first()
    serialized = str(audit.snapshot)
    assert "private/resume" not in serialized and "secret" not in serialized


def test_media_isolation_requires_two_distinct_admins_and_never_returns_key_hash(db):
    resume = _resume(db, images=["private/resume/photo.jpg"])
    issue = ResumeMediaIsolationIssue(
        resume_id=resume.id,
        key_hash=__import__("hashlib").sha256(b"private/resume/photo.jpg").hexdigest(),
        issue_type="shared_reference", status="open",
    )
    db.add(issue)
    db.commit()
    approve_media_issue(
        db, issue_id=issue.id, disposition="detach_reference", reason="verified",
        operator="admin-a",
    )
    with pytest.raises(BusinessException, match="four_eyes"):
        execute_media_issue(db, issue_id=issue.id, operator="admin-a")
    result = execute_media_issue(db, issue_id=issue.id, operator="admin-b")
    assert result["status"] == "resolved"
    assert resume.images == [] and resume.version == 2
    assert "key_hash" not in list_media_issues(db, status=None, limit=10)[0]


def test_rollout_update_uses_revision_and_audit(db):
    current = get_allowlist(db)
    updated = update_allowlist(
        db, expected_revision=current.revision, userids=["worker-1"],
        reason="扩大灰度验证", operator="root",
    )
    assert updated.revision == current.revision + 1
    assert get_allowlist(db).userids == ("worker-1",)
    with pytest.raises(BusinessException, match="revision_conflict"):
        update_allowlist(
            db, expected_revision=current.revision, userids=[],
            reason="旧版本冲突", operator="root",
        )
    audit = db.query(AuditLog).filter(
        AuditLog.reason.like("resume_replacement_rollout_update:reason_sha256=%")
    ).one()
    serialized = f"{audit.reason}|{audit.snapshot}"
    assert "worker-1" not in serialized and "扩大灰度验证" not in serialized
    assert audit.snapshot == {
        "before": {"revision": current.revision, "member_count": 0},
        "after": {"revision": updated.revision, "member_count": 1},
    }


def test_rollout_reason_rejects_sensitive_content(db):
    with pytest.raises(BusinessException, match="sensitive_data_forbidden"):
        update_allowlist(
            db, expected_revision=1, userids=[],
            reason="详情见 https://example.test/a", operator="root",
        )


def test_media_dead_letter_query_exposes_only_safe_operational_fields(db):
    asset = MediaAssetLifecycle(
        object_key="private/resume/hidden.jpg", owner_userid="worker-1",
        entity_type="resume", entity_id=9, state="dead_letter", attempt_count=10,
        last_error="contains private URL",
    )
    db.add(asset)
    db.commit()
    item = list_media_dead_letters(db, limit=10)[0]
    assert item["id"] == asset.id and item["status"] == "dead_letter"
    assert set(item) == {
        "id", "status", "attempt_count", "next_attempt_at",
        "lease_expires_at", "updated_at",
    }
    assert "private" not in str(item)


def test_redrive_quota_counts_commits_and_rollback_does_not_consume(db, monkeypatch):
    tasks = [TargetCleanupTask(
        operation_id=f"quota-{index}", target_type="resume", target_id=100 + index,
        reason="expired", status="dead_letter", attempt_count=10,
    ) for index in range(4)]
    db.add_all(tasks)
    db.commit()

    from app.services import cleanup_admin_service as service

    original_log = service.write_admin_log

    def fail_log(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(service, "write_admin_log", fail_log)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        service.redrive_dead_letters(
            db, kind="target", ids=[tasks[0].id], reason="failed", operator="root",
        )
    db.rollback()
    monkeypatch.setattr(service, "write_admin_log", original_log)

    for task in tasks[1:3]:
        service.redrive_dead_letters(
            db, kind="target", ids=[task.id], reason="committed", operator="root",
        )
    with pytest.raises(BusinessException, match="rate_limited"):
        service.redrive_dead_letters(
            db, kind="target", ids=[tasks[3].id], reason="third", operator="root",
        )
    db.rollback()
    assert db.get(TargetCleanupTask, tasks[0].id).status == "dead_letter"
