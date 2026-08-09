from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.models import (
    AuditLog,
    Job,
    JobReplacement,
    MediaAssetLifecycle,
    TargetCleanupTask,
    User,
)
from app.core.exceptions import BusinessException
from app.services.job_admin_service import (
    delist,
    extend,
    list_jobs,
    replacement_projections,
    restore,
    update_job,
)
from app.services import job_mutation_service
from app.services.job_mutation_service import close_active_replacement
from app.services.job_replacement_lock_service import ReplacementGraphHint
from app.tasks.job_candidate_cleanup import cleanup_candidate, process_due_candidates
from app.tasks import job_candidate_cleanup


@compiles(mysql.TINYINT, "sqlite")
@compiles(mysql.SMALLINT, "sqlite")
@compiles(mysql.INTEGER, "sqlite")
@compiles(mysql.BIGINT, "sqlite")
def _compile_mysql_integer_for_sqlite(_type, _compiler, **_kwargs):
    return "INTEGER"


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    for table in (
        User.__table__,
        Job.__table__,
        AuditLog.__table__,
        JobReplacement.__table__,
        MediaAssetLifecycle.__table__,
        TargetCleanupTask.__table__,
    ):
        table.create(engine)
    session = sessionmaker(bind=engine)()
    session.add(User(external_userid="owner", role="factory"))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _job(db, **overrides):
    values = {
        "owner_userid": "owner",
        "city": "苏州",
        "job_category": "普工",
        "salary_floor_monthly": 5000,
        "pay_type": "月薪",
        "headcount": 10,
        "gender_required": "不限",
        "is_long_term": True,
        "raw_text": "岗位",
        "audit_status": "pending",
        "activated_at": None,
        "expires_at": None,
        "candidate_expires_at": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1),
        "version": 1,
    }
    values.update(overrides)
    row = Job(**values)
    db.add(row)
    db.commit()
    return row


def _replacement(db, old, candidate, **overrides):
    values = {
        "operation_id": f"op-{candidate.id}",
        "source_msg_id": f"msg-{candidate.id}",
        "owner_userid": "owner",
        "old_job_id": old.id,
        "new_job_id": candidate.id,
        "old_job_version": old.version,
        "old_expires_at": old.expires_at,
        "old_business_digest": "a" * 64,
        "old_business_digest_version": 1,
        "review_outcome": "pending",
        "lifecycle_status": "awaiting_review",
        "active_old_job_id": old.id,
    }
    values.update(overrides)
    row = JobReplacement(**values)
    db.add(row)
    db.commit()
    return row


def test_cleanup_first_publish_candidate_moves_media_and_creates_task(db):
    candidate = _job(db)
    media = MediaAssetLifecycle(
        object_key="jobs/candidate.jpg", owner_userid="owner",
        entity_type="job", entity_id=candidate.id, state="attached",
    )
    db.add(media)
    db.commit()
    expiry = candidate.candidate_expires_at

    assert cleanup_candidate(db, candidate.id)
    db.commit()

    assert candidate.deleted_at is not None
    assert candidate.delist_reason is None
    assert candidate.candidate_expires_at == expiry
    assert candidate.version == 2
    assert media.state == "delete_pending"
    assert db.query(TargetCleanupTask).filter_by(target_id=candidate.id).one().reason == "candidate_expired"


@pytest.mark.parametrize(
    "review_outcome,lifecycle_status,audit_status,closed_before,closed_after",
    [
        ("rejected", "closed", "rejected", "rejected", "rejected"),
        ("passed", "conflict", "pending", None, "candidate_expired"),
    ],
)
def test_cleanup_replacement_preserves_review_outcome_and_closure_reason(
    db, review_outcome, lifecycle_status, audit_status, closed_before, closed_after,
):
    old = _job(
        db,
        audit_status="passed",
        activated_at=datetime.now(),
        expires_at=datetime.now() + timedelta(days=10),
        candidate_expires_at=None,
    )
    candidate = _job(db, audit_status=audit_status)
    relation = _replacement(
        db,
        old,
        candidate,
        review_outcome=review_outcome,
        lifecycle_status=lifecycle_status,
        closed_reason=closed_before,
        active_old_job_id=old.id if lifecycle_status == "conflict" else None,
    )

    assert cleanup_candidate(db, candidate.id)
    db.commit()

    assert relation.review_outcome == review_outcome
    assert relation.lifecycle_status == "closed"
    assert relation.closed_reason == closed_after
    assert relation.candidate_cleaned_at is not None
    assert relation.active_old_job_id is None
    assert old.deleted_at is None


def test_cleanup_skips_activated_and_passed_without_expiry_invariant(db):
    active = _job(
        db,
        audit_status="passed",
        activated_at=datetime.now(),
        expires_at=datetime.now() + timedelta(days=10),
        candidate_expires_at=None,
    )
    anomaly = _job(db, audit_status="passed")

    assert cleanup_candidate(db, active.id) is False
    assert cleanup_candidate(db, anomaly.id) is False
    assert process_due_candidates(db)["cleaned"] == 0
    assert active.deleted_at is None and anomaly.deleted_at is None


def test_lifecycle_scopes_and_replacement_projection(db):
    active = _job(
        db,
        audit_status="passed",
        activated_at=datetime.now(),
        expires_at=datetime.now() + timedelta(days=10),
        candidate_expires_at=None,
    )
    candidate = _job(db, candidate_expires_at=datetime.now() + timedelta(days=7))
    relation = _replacement(db, active, candidate)
    history = _job(db, deleted_at=datetime.now())

    active_rows, _ = list_jobs(db, {}, lifecycle_scope="active")
    candidate_rows, _ = list_jobs(db, {}, lifecycle_scope="candidate")
    history_rows, _ = list_jobs(db, {}, lifecycle_scope="history")
    all_rows, _ = list_jobs(db, {}, lifecycle_scope="all")
    default_rows, _ = list_jobs(db, {})
    projection = replacement_projections(db, [active, candidate])

    assert [row.id for row in active_rows] == [active.id]
    assert [row.id for row in candidate_rows] == [candidate.id]
    assert [row.id for row in history_rows] == [history.id]
    assert len(all_rows) == 3
    assert {row.id for row in default_rows} == {active.id, candidate.id}
    assert projection[candidate.id]["replacement_id"] == relation.id
    assert projection[candidate.id]["replaces_job_id"] == active.id
    assert projection[active.id]["replaced_by_job_id"] == candidate.id


@pytest.mark.parametrize("operation", ["edit", "extend", "delist", "restore"])
def test_admin_online_operations_reject_candidate(db, operation):
    candidate = _job(db)

    with pytest.raises(BusinessException, match="job_not_activated"):
        if operation == "edit":
            update_job(db, candidate.id, candidate.version, {"city": "无锡"}, "admin")
        elif operation == "extend":
            extend(db, candidate.id, candidate.version, 15, "admin")
        elif operation == "delist":
            delist(db, candidate.id, candidate.version, "manual_delist", "admin")
        else:
            restore(db, candidate.id, candidate.version, "admin")


def test_restore_rejects_replaced_and_soft_deleted_jobs_without_state_changes(db):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    replaced = _job(
        db,
        audit_status="passed",
        activated_at=now,
        expires_at=now + timedelta(days=10),
        candidate_expires_at=None,
        delist_reason="replaced",
    )
    deleted = _job(
        db,
        audit_status="passed",
        activated_at=now,
        expires_at=now + timedelta(days=10),
        candidate_expires_at=None,
        delist_reason="manual_delist",
        deleted_at=now,
    )
    cleanup = TargetCleanupTask(
        operation_id="restore-blocked-cleanup",
        target_type="job",
        target_id=deleted.id,
        reason="manual_delist",
        reason_history=["manual_delist"],
        status="retry_wait",
    )
    db.add(cleanup)
    db.commit()

    for job in (replaced, deleted):
        original_version = job.version
        with pytest.raises(BusinessException, match="job_not_restorable"):
            restore(db, job.id, job.version, "admin")
        assert job.version == original_version

    assert replaced.delist_reason == "replaced"
    assert replaced.deleted_at is None
    assert deleted.delist_reason == "manual_delist"
    assert deleted.deleted_at == now
    assert cleanup.status == "retry_wait"
    assert cleanup.reason_history == ["manual_delist"]


def test_restore_allows_ordinary_delisted_job(db):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    job = _job(
        db,
        audit_status="passed",
        activated_at=now,
        expires_at=now + timedelta(days=10),
        candidate_expires_at=None,
        delist_reason="manual_delist",
        deleted_at=None,
    )
    original_version = job.version

    restore(db, job.id, job.version, "admin")

    assert job.delist_reason is None
    assert job.deleted_at is None
    assert job.version == original_version + 1


def test_delisting_old_job_closes_candidate_with_graph_lock_cleanup(db):
    old = _job(
        db,
        audit_status="passed",
        activated_at=datetime.now(),
        expires_at=datetime.now() + timedelta(days=10),
        candidate_expires_at=None,
    )
    candidate = _job(db, candidate_expires_at=datetime.now() + timedelta(days=7))
    relation = _replacement(db, old, candidate)
    media = MediaAssetLifecycle(
        object_key="jobs/closed-candidate.jpg", owner_userid="owner",
        entity_type="job", entity_id=candidate.id, state="attached",
    )
    db.add(media)
    db.commit()

    close_active_replacement(db, old, reason="old_job_delisted")
    db.commit()

    assert relation.lifecycle_status == "closed"
    assert relation.closed_reason == "old_job_delisted"
    assert relation.active_old_job_id is None
    assert candidate.deleted_at is not None
    assert media.state == "delete_pending"
    assert db.query(TargetCleanupTask).filter_by(target_id=candidate.id).one().reason == "candidate_cancelled"


def test_close_active_replacement_uses_current_hint_before_graph_lock(monkeypatch):
    calls = []
    old = MagicMock(id=10)
    candidate = MagicMock(id=11, expires_at=datetime.now(), deleted_at=None)
    relation = JobReplacement(
        id=7,
        operation_id="op-lock-order",
        source_msg_id="msg-lock-order",
        owner_userid="owner",
        old_job_id=10,
        new_job_id=11,
        old_job_version=1,
        old_business_digest="digest",
        old_business_digest_version=1,
        review_outcome="pending",
        lifecycle_status="awaiting_review",
        active_old_job_id=10,
    )
    hint = ReplacementGraphHint(7, 10, 11, "op-lock-order")

    def current_hint(_db, _old_job_id):
        calls.append("current_hint")
        return hint

    def graph_lock(_db, _replacement_id, *, hint):
        calls.append("graph_lock")
        return relation, [old, candidate], {10: old, 11: candidate}

    monkeypatch.setattr(job_mutation_service, "current_active_replacement_hint", current_hint)
    monkeypatch.setattr(job_mutation_service, "lock_replacement_graph", graph_lock)
    monkeypatch.setattr(
        job_mutation_service,
        "lock_job_for_mutation",
        lambda *_args: pytest.fail("existing graph must use the global graph lock directly"),
    )

    locked = close_active_replacement(MagicMock(), old, reason="old_job_delisted")

    assert locked is old
    assert calls == ["current_hint", "graph_lock"]


def test_close_active_replacement_rechecks_creation_race_after_old_job_lock(monkeypatch):
    calls = []
    old = MagicMock(id=10)
    candidate = MagicMock(id=11, expires_at=datetime.now(), deleted_at=None)
    relation = JobReplacement(
        id=8,
        operation_id="op-race",
        source_msg_id="msg-race",
        owner_userid="owner",
        old_job_id=10,
        new_job_id=11,
        old_job_version=1,
        old_business_digest="digest",
        old_business_digest_version=1,
        review_outcome="pending",
        lifecycle_status="awaiting_review",
        active_old_job_id=10,
    )
    hint = ReplacementGraphHint(8, 10, 11, "op-race")
    hints = iter((None, hint))

    def current_hint(_db, _old_job_id):
        calls.append("current_hint")
        return next(hints)

    def old_lock(_db, _old_job_id):
        calls.append("old_job_lock")
        return old

    def graph_lock(_db, _replacement_id, *, hint):
        calls.append("graph_lock")
        return relation, [old, candidate], {10: old, 11: candidate}

    monkeypatch.setattr(job_mutation_service, "current_active_replacement_hint", current_hint)
    monkeypatch.setattr(job_mutation_service, "lock_job_for_mutation", old_lock)
    monkeypatch.setattr(job_mutation_service, "lock_replacement_graph", graph_lock)

    locked = close_active_replacement(MagicMock(), old, reason="old_job_delisted")

    assert locked is old
    assert calls == ["current_hint", "old_job_lock", "current_hint", "graph_lock"]


def test_candidate_cleanup_feature_switch_skips_lock(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "job_candidate_cleanup_enabled", False)
    lock = MagicMock()
    monkeypatch.setattr(job_candidate_cleanup, "renewable_task_lock", lock)

    job_candidate_cleanup.run()

    lock.assert_not_called()


def test_candidate_cleanup_renews_each_batch(db):
    _job(db)
    lease = MagicMock()
    lease.renew.return_value = True

    stats = process_due_candidates(
        db, batch_size=1, max_runtime_seconds=None, lease=lease
    )

    assert stats["cleaned"] == 1
    lease.renew.assert_called_once_with()


def test_candidate_cleanup_schedules_continuation_at_runtime_limit(monkeypatch):
    continuation = MagicMock()
    monkeypatch.setattr(
        job_candidate_cleanup.time, "monotonic", MagicMock(side_effect=[0, 481])
    )

    stats = process_due_candidates(
        MagicMock(), max_runtime_seconds=480, continuation=continuation
    )

    assert stats["continuation_scheduled"] is True
    continuation.assert_called_once_with()
