"""Durable, checkpointed invalidation of recommendation targets."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Job, TargetCleanupTask


TARGET_CLEANUP_LEASE = timedelta(minutes=4)
TARGET_CLEANUP_MAX_ATTEMPTS = 10
_PROCESSABLE_STATUSES = ("pending", "retry_wait", "processing")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _lock_job_cleanup_task(
    db: Session, job_id: int,
) -> TargetCleanupTask | None:
    return (
        db.query(TargetCleanupTask)
        .populate_existing()
        .filter(
            TargetCleanupTask.target_type == "job",
            TargetCleanupTask.target_id == job_id,
        )
        .with_for_update()
        .first()
    )


def upsert_job_cleanup_task(
    db: Session,
    job_id: int,
    *,
    reason: str,
    operation_id: str | None = None,
) -> tuple[TargetCleanupTask, bool]:
    # The Job row serializes creation while no target_cleanup_task row exists.
    (
        db.query(Job.id)
        .populate_existing()
        .filter(Job.id == job_id)
        .with_for_update()
        .one()
    )
    task = _lock_job_cleanup_task(db, job_id)
    created = False
    if task is None:
        task = TargetCleanupTask(
            operation_id=operation_id or str(uuid.uuid4()),
            target_type="job",
            target_id=job_id,
            reason=reason,
            reason_history=[reason],
            status="pending",
        )
        # Flush unrelated outer work before opening the savepoint so an insert
        # race cannot roll back the caller's whole transaction.
        db.flush()
        try:
            with db.begin_nested():
                db.add(task)
                db.flush()
            created = True
        except IntegrityError:
            task = _lock_job_cleanup_task(db, job_id)
            if task is None:
                raise

    history = list(task.reason_history or [])
    if task.reason and task.reason not in history:
        history.append(task.reason)
    if reason not in history:
        history.append(reason)
    task.reason_history = history
    return task, created


def ensure_job_cleanup_task(
    db: Session,
    job_id: int,
    *,
    reason: str,
    operation_id: str | None = None,
) -> TargetCleanupTask:
    task, _ = upsert_job_cleanup_task(
        db,
        job_id,
        reason=reason,
        operation_id=operation_id,
    )
    return task


def job_cleanup_succeeded(db: Session, job_id: int) -> bool:
    task = db.query(TargetCleanupTask).filter_by(target_type="job", target_id=job_id).first()
    return bool(task and task.status == "succeeded")


def claim_cleanup_tasks(
    db: Session,
    owner: str,
    now: datetime,
    limit: int,
) -> list[int]:
    rows = (
        db.query(TargetCleanupTask)
        .filter(
            TargetCleanupTask.status.in_(_PROCESSABLE_STATUSES),
            (TargetCleanupTask.next_attempt_at.is_(None))
            | (TargetCleanupTask.next_attempt_at <= now),
            (TargetCleanupTask.lease_expires_at.is_(None))
            | (TargetCleanupTask.lease_expires_at <= now),
        )
        .order_by(TargetCleanupTask.id)
        .with_for_update(skip_locked=True)
        .limit(limit)
        .all()
    )
    for row in rows:
        row.status = "processing"
        row.attempt_count = int(row.attempt_count or 0) + 1
        row.next_attempt_at = None
        row.lease_owner = owner
        row.lease_expires_at = now + TARGET_CLEANUP_LEASE
    ids = [int(row.id) for row in rows]
    db.commit()
    return ids


def _lock_claimed_task(
    db: Session,
    task_id: int,
    owner: str,
    now: datetime,
) -> TargetCleanupTask | None:
    return (
        db.query(TargetCleanupTask)
        .populate_existing()
        .filter(
            TargetCleanupTask.id == task_id,
            TargetCleanupTask.status == "processing",
            TargetCleanupTask.lease_owner == owner,
            TargetCleanupTask.lease_expires_at.isnot(None),
            TargetCleanupTask.lease_expires_at > now,
        )
        .with_for_update()
        .first()
    )


def renew_cleanup_task_lease(
    db: Session,
    task_id: int,
    owner: str,
    now: datetime,
) -> dict | None:
    task = _lock_claimed_task(db, task_id, owner, now)
    if task is None:
        db.rollback()
        return None
    snapshot = {
        "target_type": task.target_type,
        "target_id": int(task.target_id),
        "delivery_ids": list(task.delivery_ids or []),
        "db_redacted_at": task.db_redacted_at,
        "conversation_redacted_at": task.conversation_redacted_at,
        "session_invalidated_at": task.session_invalidated_at,
    }
    task.lease_expires_at = now + TARGET_CLEANUP_LEASE
    db.commit()
    return snapshot


def checkpoint_cleanup_task(
    db: Session,
    task_id: int,
    owner: str,
    checkpoint: str,
    now: datetime,
    *,
    delivery_ids: list[str] | None = None,
) -> bool:
    if checkpoint not in {
        "db_redacted_at",
        "conversation_redacted_at",
        "session_invalidated_at",
    }:
        raise ValueError(f"invalid target cleanup checkpoint: {checkpoint}")
    task = _lock_claimed_task(db, task_id, owner, now)
    if task is None:
        db.rollback()
        return False
    if delivery_ids is not None:
        task.delivery_ids = sorted(set(delivery_ids))
    setattr(task, checkpoint, now)
    task.lease_expires_at = now + TARGET_CLEANUP_LEASE
    db.commit()
    return True


def complete_cleanup_task(
    db: Session,
    task_id: int,
    owner: str,
    now: datetime,
) -> bool:
    task = _lock_claimed_task(db, task_id, owner, now)
    if task is None:
        db.rollback()
        return False
    task.status = "succeeded"
    task.completed_at = now
    task.last_error = None
    task.next_attempt_at = None
    task.lease_owner = None
    task.lease_expires_at = None
    db.commit()
    return True


def fail_cleanup_task(
    db: Session,
    task_id: int,
    owner: str,
    error: Exception,
    now: datetime,
) -> bool:
    task = _lock_claimed_task(db, task_id, owner, now)
    if task is None:
        db.rollback()
        return False
    attempts = int(task.attempt_count or 1)
    task.status = (
        "dead_letter"
        if attempts >= TARGET_CLEANUP_MAX_ATTEMPTS
        else "retry_wait"
    )
    task.last_error = str(error)[:255]
    task.next_attempt_at = (
        None
        if task.status == "dead_letter"
        else now + timedelta(seconds=min(3600, 2 ** min(attempts, 10)))
    )
    task.lease_owner = None
    task.lease_expires_at = None
    db.commit()
    return True


def process_cleanup_task(db: Session, task_id: int, owner: str) -> bool:
    try:
        from app.services.recommendation_privacy_service import (
            TargetRef,
            redact_conversation_logs,
            redact_deliveries_for_targets,
            scrub_recommendation_sessions,
        )

        # Keep the durable task lock while each database stage acquires its
        # downstream outbox/delivery locks. This preserves the global order
        # TargetCleanupTask -> outbox -> RecommendationDelivery.
        now = _utcnow()
        task = _lock_claimed_task(db, task_id, owner, now)
        if task is None:
            db.rollback()
            return False
        task.lease_expires_at = now + TARGET_CLEANUP_LEASE
        target = TargetRef(task.target_type, int(task.target_id))
        if task.db_redacted_at is None:
            deliveries = redact_deliveries_for_targets(db, [target], commit=False)
            checkpoint_at = _utcnow()
            if (
                task.lease_expires_at is None
                or task.lease_expires_at <= checkpoint_at
            ):
                db.rollback()
                return False
            task.delivery_ids = sorted(set(deliveries))
            task.db_redacted_at = checkpoint_at
            task.lease_expires_at = checkpoint_at + TARGET_CLEANUP_LEASE
        db.commit()

        now = _utcnow()
        task = _lock_claimed_task(db, task_id, owner, now)
        if task is None:
            db.rollback()
            return False
        task.lease_expires_at = now + TARGET_CLEANUP_LEASE
        if task.conversation_redacted_at is None:
            redact_conversation_logs(
                db, list(task.delivery_ids or []), commit=False,
            )
            checkpoint_at = _utcnow()
            if (
                task.lease_expires_at is None
                or task.lease_expires_at <= checkpoint_at
            ):
                db.rollback()
                return False
            task.conversation_redacted_at = checkpoint_at
            task.lease_expires_at = checkpoint_at + TARGET_CLEANUP_LEASE
        db.commit()

        snapshot = renew_cleanup_task_lease(db, task_id, owner, _utcnow())
        if snapshot is None:
            return False
        target = TargetRef(snapshot["target_type"], snapshot["target_id"])
        if snapshot["session_invalidated_at"] is None:
            scrub_recommendation_sessions(
                snapshot["delivery_ids"], [target],
            )
            if not checkpoint_cleanup_task(
                db,
                task_id,
                owner,
                "session_invalidated_at",
                _utcnow(),
            ):
                return False

        return complete_cleanup_task(db, task_id, owner, _utcnow())
    except Exception as exc:
        db.rollback()
        fail_cleanup_task(db, task_id, owner, exc, _utcnow())
        return False
