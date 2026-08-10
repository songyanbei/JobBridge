from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateTable

from app.models import TargetCleanupTask
from app.services import recommendation_privacy_service, target_cleanup_service


@compiles(mysql.TINYINT, "sqlite")
@compiles(mysql.SMALLINT, "sqlite")
@compiles(mysql.INTEGER, "sqlite")
@compiles(mysql.BIGINT, "sqlite")
def _compile_mysql_integer_for_sqlite(_type, _compiler, **_kwargs):
    return "INTEGER"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(CreateTable(TargetCleanupTask.__table__))
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _task(**overrides) -> TargetCleanupTask:
    values = {
        "operation_id": str(uuid4()),
        "target_type": "job",
        "target_id": int(uuid4().int % 1_000_000),
        "reason": "expired",
        "reason_history": ["expired"],
        "status": "pending",
        "attempt_count": 0,
    }
    values.update(overrides)
    return TargetCleanupTask(**values)


def test_claim_sets_owner_processing_state_and_attempt(db):
    now = _now()
    due = _task(next_attempt_at=now - timedelta(seconds=1))
    stale = _task(
        status="processing",
        attempt_count=2,
        lease_owner="old-owner",
        lease_expires_at=now - timedelta(seconds=1),
    )
    active = _task(
        status="processing",
        lease_owner="active-owner",
        lease_expires_at=now + timedelta(minutes=1),
    )
    future = _task(
        status="retry_wait",
        next_attempt_at=now + timedelta(minutes=1),
    )
    terminal = _task(status="dead_letter", attempt_count=10)
    db.add_all([due, stale, active, future, terminal])
    db.commit()

    claimed = target_cleanup_service.claim_cleanup_tasks(
        db, "worker-1", now, 100,
    )

    assert set(claimed) == {due.id, stale.id}
    db.refresh(due)
    db.refresh(stale)
    assert due.status == "processing" and due.attempt_count == 1
    assert stale.status == "processing" and stale.attempt_count == 3
    assert due.lease_owner == stale.lease_owner == "worker-1"
    assert due.lease_expires_at == now + target_cleanup_service.TARGET_CLEANUP_LEASE


@pytest.mark.parametrize(
    ("lease_owner", "lease_delta"),
    [("other-owner", 60), ("worker-1", -1)],
)
def test_renew_rejects_stolen_or_expired_lease(db, lease_owner, lease_delta):
    now = _now()
    task = _task(
        status="processing",
        lease_owner=lease_owner,
        lease_expires_at=now + timedelta(seconds=lease_delta),
    )
    db.add(task)
    db.commit()

    snapshot = target_cleanup_service.renew_cleanup_task_lease(
        db, task.id, "worker-1", now,
    )

    assert snapshot is None
    db.refresh(task)
    assert task.lease_owner == lease_owner


@pytest.mark.parametrize(
    ("lease_owner", "lease_delta"),
    [("other-owner", 60), ("worker-1", -1)],
)
def test_checkpoint_rejects_lost_lease_and_rolls_back_stage(
    db, lease_owner, lease_delta,
):
    now = _now()
    task = _task(
        status="processing",
        lease_owner=lease_owner,
        lease_expires_at=now + timedelta(seconds=lease_delta),
    )
    db.add(task)
    db.commit()
    task.reason_history = ["expired", "uncommitted-stage"]

    saved = target_cleanup_service.checkpoint_cleanup_task(
        db,
        task.id,
        "worker-1",
        "db_redacted_at",
        now,
        delivery_ids=["delivery-1"],
    )

    assert saved is False
    db.refresh(task)
    assert task.db_redacted_at is None
    assert task.delivery_ids is None
    assert task.reason_history == ["expired"]


@pytest.mark.parametrize(
    ("transition", "lease_owner", "lease_delta"),
    [
        ("success", "other-owner", 60),
        ("success", "worker-1", -1),
        ("failure", "other-owner", 60),
        ("failure", "worker-1", -1),
    ],
)
def test_terminal_updates_reject_stolen_or_expired_lease(
    db, transition, lease_owner, lease_delta,
):
    now = _now()
    task = _task(
        status="processing",
        attempt_count=3,
        lease_owner=lease_owner,
        lease_expires_at=now + timedelta(seconds=lease_delta),
    )
    db.add(task)
    db.commit()

    if transition == "success":
        saved = target_cleanup_service.complete_cleanup_task(
            db, task.id, "worker-1", now,
        )
    else:
        saved = target_cleanup_service.fail_cleanup_task(
            db, task.id, "worker-1", RuntimeError("failed"), now,
        )

    assert saved is False
    db.refresh(task)
    assert task.status == "processing"
    assert task.attempt_count == 3
    assert task.last_error is None


@pytest.mark.parametrize(
    ("attempt_count", "expected_status"),
    [(9, "retry_wait"), (10, "dead_letter")],
)
def test_failure_transition_is_fenced_and_preserves_attempt_count(
    db, attempt_count, expected_status,
):
    now = _now()
    task = _task(
        status="processing",
        attempt_count=attempt_count,
        lease_owner="worker-1",
        lease_expires_at=now + timedelta(minutes=1),
    )
    db.add(task)
    db.commit()

    saved = target_cleanup_service.fail_cleanup_task(
        db, task.id, "worker-1", RuntimeError("cleanup failed"), now,
    )

    assert saved is True
    db.refresh(task)
    assert task.status == expected_status
    assert task.attempt_count == attempt_count
    assert task.lease_owner is None and task.lease_expires_at is None
    assert (task.next_attempt_at is None) == (expected_status == "dead_letter")


def test_checkpoint_and_success_with_current_lease(db):
    now = _now()
    task = _task(
        status="processing",
        attempt_count=1,
        lease_owner="worker-1",
        lease_expires_at=now + timedelta(minutes=1),
    )
    db.add(task)
    db.commit()

    assert target_cleanup_service.checkpoint_cleanup_task(
        db,
        task.id,
        "worker-1",
        "db_redacted_at",
        now,
        delivery_ids=["delivery-2", "delivery-1", "delivery-1"],
    )
    assert target_cleanup_service.complete_cleanup_task(
        db, task.id, "worker-1", now + timedelta(seconds=1),
    )

    db.refresh(task)
    assert task.delivery_ids == ["delivery-1", "delivery-2"]
    assert task.db_redacted_at == now
    assert task.status == "succeeded"
    assert task.lease_owner is None and task.lease_expires_at is None


def test_process_renews_and_checkpoints_each_stage(db, monkeypatch):
    now = _now()
    task = _task(
        status="processing",
        attempt_count=1,
        lease_owner="worker-1",
        lease_expires_at=now + timedelta(minutes=1),
    )
    db.add(task)
    db.commit()
    calls = []
    lock_order = []
    real_lock = target_cleanup_service._lock_claimed_task

    def _record_task_lock(*args, **kwargs):
        lock_order.append("target_task")
        return real_lock(*args, **kwargs)

    monkeypatch.setattr(
        target_cleanup_service, "_lock_claimed_task", _record_task_lock,
    )

    def _redact_deliveries(_db, targets, *, commit):
        lock_order.append("delivery_redaction")
        calls.append((
            "deliveries",
            targets[0].target_type,
            targets[0].target_id,
            commit,
        ))
        return {"delivery-2", "delivery-1"}

    def _redact_conversations(_db, delivery_ids, *, commit):
        calls.append(("conversations", list(delivery_ids), commit))
        return 2

    def _scrub_sessions(delivery_ids, targets):
        calls.append(("sessions", list(delivery_ids), targets[0].target_id))
        return 1

    monkeypatch.setattr(
        recommendation_privacy_service,
        "redact_deliveries_for_targets",
        _redact_deliveries,
    )
    monkeypatch.setattr(
        recommendation_privacy_service,
        "redact_conversation_logs",
        _redact_conversations,
    )
    monkeypatch.setattr(
        recommendation_privacy_service,
        "scrub_recommendation_sessions",
        _scrub_sessions,
    )

    assert target_cleanup_service.process_cleanup_task(
        db, task.id, "worker-1",
    )

    db.refresh(task)
    assert task.status == "succeeded"
    assert task.delivery_ids == ["delivery-1", "delivery-2"]
    assert task.db_redacted_at is not None
    assert task.conversation_redacted_at is not None
    assert task.session_invalidated_at is not None
    assert calls == [
        ("deliveries", "job", task.target_id, False),
        ("conversations", ["delivery-1", "delivery-2"], False),
        ("sessions", ["delivery-1", "delivery-2"], task.target_id),
    ]
    assert lock_order[:2] == ["target_task", "delivery_redaction"]


def test_process_rolls_back_database_stage_when_lease_expires(db, monkeypatch):
    now = _now()
    task = _task(
        status="processing",
        attempt_count=1,
        lease_owner="worker-1",
        lease_expires_at=now + timedelta(minutes=1),
    )
    db.add(task)
    db.commit()

    moments = iter([
        now,
        now + target_cleanup_service.TARGET_CLEANUP_LEASE + timedelta(seconds=1),
    ])
    monkeypatch.setattr(target_cleanup_service, "_utcnow", lambda: next(moments))

    def _redact_deliveries(stage_db, _targets, *, commit):
        assert commit is False
        stage_task = stage_db.get(TargetCleanupTask, task.id)
        stage_task.reason_history = ["expired", "uncommitted-stage"]
        return {"delivery-1"}

    monkeypatch.setattr(
        recommendation_privacy_service,
        "redact_deliveries_for_targets",
        _redact_deliveries,
    )

    assert not target_cleanup_service.process_cleanup_task(
        db, task.id, "worker-1",
    )

    db.refresh(task)
    assert task.db_redacted_at is None
    assert task.delivery_ids is None
    assert task.reason_history == ["expired"]
