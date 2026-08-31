"""Expire unactivated first-publish and replacement Job candidates."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable

from loguru import logger
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import Job, JobReplacement
from app.services.job_media_service import mark_job_media_delete_pending
from app.services.job_mutation_service import increment_version
from app.services.job_replacement_lock_service import lock_replacement_graph
from app.services.target_cleanup_service import ensure_job_cleanup_task
from app.tasks.common import log_event, renewable_task_lock

BATCH_SIZE = 500
MAX_RUNTIME_SECONDS = 8 * 60


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _is_due_candidate(candidate: Job, now: datetime) -> bool:
    return bool(
        candidate.audit_status in {"pending", "rejected"}
        and candidate.activated_at is None
        and candidate.expires_at is None
        and candidate.candidate_expires_at is not None
        and candidate.candidate_expires_at <= now
        and candidate.deleted_at is None
    )


def cleanup_candidate(db: Session, candidate_id: int, *, now: datetime | None = None) -> bool:
    """Lock one candidate with the shared graph order and move it to cleanup state."""
    now = now or _utcnow()
    relation_hint = db.query(JobReplacement).filter(
        JobReplacement.new_job_id == candidate_id,
    ).first()
    relation = None
    if relation_hint is not None:
        relation, _, jobs = lock_replacement_graph(db, relation_hint.id)
        candidate = jobs.get(candidate_id) if jobs else None
    else:
        candidate = db.query(Job).filter(Job.id == candidate_id).with_for_update().first()

    if candidate is None or not _is_due_candidate(candidate, now):
        return False

    if relation is not None:
        relation.lifecycle_status = "closed"
        relation.candidate_cleaned_at = now
        relation.active_old_job_id = None
        if not relation.closed_reason:
            relation.closed_reason = "candidate_expired"

    candidate.deleted_at = now
    increment_version(candidate)
    # Candidate deletion is a fact-source tombstone, not only cleanup state.
    try:
        from app.services.job_lifecycle_service import _emit
        _emit(db, candidate, "job.candidate_deleted", reason="candidate_expired", tombstone=True)
    except (ImportError, TypeError, AttributeError):
        pass
    mark_job_media_delete_pending(db, candidate.id, include_pending=True)
    ensure_job_cleanup_task(db, candidate.id, reason="candidate_expired")
    db.flush()
    return True


def process_due_candidates(
    db: Session,
    *,
    now: datetime | None = None,
    batch_size: int = BATCH_SIZE,
    max_runtime_seconds: int | None = MAX_RUNTIME_SECONDS,
    lease=None,
    continuation: Callable[[], None] | None = None,
) -> dict[str, int | bool]:
    """Drain the due set once in stable candidate_expires_at/id order."""
    now = now or _utcnow()
    started = time.monotonic()
    stats: dict[str, int | bool] = {
        "cleaned": 0,
        "conflicts": 0,
        "batches": 0,
        "continuation_scheduled": False,
    }
    cursor_expiry = None
    cursor_id = 0
    while True:
        if (
            max_runtime_seconds is not None
            and time.monotonic() - started >= max_runtime_seconds
        ):
            if continuation is None:
                from app.tasks.scheduler import schedule_job_candidate_continuation
                continuation = schedule_job_candidate_continuation
            continuation()
            stats["continuation_scheduled"] = True
            break
        query = db.query(Job.id, Job.candidate_expires_at).filter(
            Job.expires_at.is_(None),
            Job.candidate_expires_at.isnot(None),
            Job.candidate_expires_at <= now,
            Job.audit_status.in_(("pending", "rejected")),
            Job.deleted_at.is_(None),
        )
        if cursor_expiry is not None:
            query = query.filter(or_(
                Job.candidate_expires_at > cursor_expiry,
                and_(
                    Job.candidate_expires_at == cursor_expiry,
                    Job.id > cursor_id,
                ),
            ))
        rows = query.order_by(Job.candidate_expires_at, Job.id).limit(batch_size).all()
        if not rows:
            break
        stats["batches"] += 1
        for candidate_id, candidate_expiry in rows:
            try:
                if cleanup_candidate(db, int(candidate_id), now=now):
                    db.commit()
                    stats["cleaned"] += 1
                else:
                    db.rollback()
                    stats["conflicts"] += 1
            except Exception:
                db.rollback()
                stats["conflicts"] += 1
                logger.exception("job candidate cleanup failed: candidate_id={}", candidate_id)
            cursor_expiry, cursor_id = candidate_expiry, int(candidate_id)
        if lease is not None and not lease.renew():
            logger.error("job candidate cleanup lost distributed lease")
            break
        if len(rows) < batch_size:
            break
    return stats


def run() -> None:
    from app.config import settings
    if not settings.job_candidate_cleanup_enabled:
        log_event("job_candidate_cleanup_disabled")
        return
    with renewable_task_lock("job_candidate_cleanup", ttl=1200) as lease:
        if not lease:
            return
        with SessionLocal() as db:
            now = _utcnow()
            due_count, oldest_due = db.query(
                func.count(Job.id), func.min(Job.candidate_expires_at),
            ).filter(
                Job.expires_at.is_(None),
                Job.candidate_expires_at.isnot(None),
                Job.candidate_expires_at <= now,
                Job.audit_status.in_(("pending", "rejected")),
                Job.deleted_at.is_(None),
            ).one()
            invariant_count = db.query(Job).filter(
                Job.audit_status == "passed",
                Job.expires_at.is_(None),
                Job.deleted_at.is_(None),
            ).count()
            stats = process_due_candidates(db, now=now, lease=lease)
            log_event(
                "job_candidate_cleanup_summary",
                **stats,
                job_candidate_due_count=int(due_count or 0),
                job_candidate_oldest_lag_seconds=(
                    max(0, int((now - oldest_due).total_seconds()))
                    if oldest_due else 0
                ),
                job_candidate_cleanup_success_total=stats["cleaned"],
                job_candidate_cleanup_conflict_total=stats["conflicts"],
                job_passed_without_activation_total=invariant_count,
            )
