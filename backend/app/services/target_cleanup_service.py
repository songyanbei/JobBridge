"""Durable, checkpointed invalidation of recommendation targets."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models import TargetCleanupTask


def ensure_job_cleanup_task(
    db: Session,
    job_id: int,
    *,
    reason: str,
    operation_id: str | None = None,
) -> TargetCleanupTask:
    task = db.query(TargetCleanupTask).filter_by(target_type="job", target_id=job_id).first()
    if task is None:
        task = TargetCleanupTask(
            operation_id=operation_id or str(uuid.uuid4()),
            target_type="job",
            target_id=job_id,
            reason=reason,
            reason_history=[reason],
            status="pending",
        )
        db.add(task)
        db.flush()
    elif reason not in (task.reason_history or []):
        task.reason_history = [*(task.reason_history or [task.reason]), reason]
    return task


def job_cleanup_succeeded(db: Session, job_id: int) -> bool:
    task = db.query(TargetCleanupTask).filter_by(target_type="job", target_id=job_id).first()
    return bool(task and task.status == "succeeded")


def process_cleanup_task(db: Session, task_id: int) -> bool:
    task = db.query(TargetCleanupTask).filter(
        TargetCleanupTask.id == task_id,
    ).with_for_update().first()
    if task is None or task.status == "succeeded":
        return bool(task)
    task.status = "processing"
    task.attempt_count = int(task.attempt_count or 0) + 1
    db.commit()

    try:
        from app.services.recommendation_privacy_service import (
            TargetRef,
            redact_conversation_logs,
            redact_deliveries_for_targets,
            scrub_recommendation_sessions,
        )

        target = TargetRef(task.target_type, int(task.target_id))
        if task.db_redacted_at is None:
            deliveries = redact_deliveries_for_targets(db, [target], commit=False)
            task.delivery_ids = sorted(deliveries)
            task.db_redacted_at = datetime.utcnow()
            db.commit()
        if task.conversation_redacted_at is None:
            redact_conversation_logs(db, task.delivery_ids or [], commit=False)
            task.conversation_redacted_at = datetime.utcnow()
            db.commit()
        if task.session_invalidated_at is None:
            scrub_recommendation_sessions(task.delivery_ids or [], [target])
            task.session_invalidated_at = datetime.utcnow()
            db.commit()

        task.status = "succeeded"
        task.completed_at = datetime.utcnow()
        task.last_error = None
        task.next_attempt_at = None
        task.lease_owner = None
        task.lease_expires_at = None
        db.commit()
        return True
    except Exception as exc:
        db.rollback()
        task = db.query(TargetCleanupTask).filter(
            TargetCleanupTask.id == task_id,
        ).with_for_update().one()
        attempts = int(task.attempt_count or 1)
        task.status = "dead_letter" if attempts >= 10 else "retry_wait"
        task.last_error = str(exc)[:255]
        task.next_attempt_at = datetime.utcnow() + timedelta(
            seconds=min(3600, 2 ** min(attempts, 10))
        )
        task.lease_owner = None
        task.lease_expires_at = None
        db.commit()
        return False
