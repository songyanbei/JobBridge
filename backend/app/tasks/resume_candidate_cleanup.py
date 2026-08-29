"""Recover expired first-upload and replacement Resume candidates."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable

from loguru import logger
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import Resume, ResumeReplacement
from app.services.job_media_service import mark_resume_media_delete_pending
from app.services.resume_mutation_service import increment_resume_version
from app.services.resume_replacement_lock_service import lock_replacement_graph
from app.services.target_cleanup_service import ensure_target_cleanup_task
from app.tasks.common import log_event, renewable_task_lock

BATCH_SIZE = 500
MAX_RUNTIME_SECONDS = 8 * 60


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _is_due(row: Resume, now: datetime) -> bool:
    return bool(
        row.audit_status in {"pending", "rejected"}
        and row.activated_at is None and row.expires_at is None
        and row.candidate_expires_at is not None and row.candidate_expires_at <= now
        and row.deleted_at is None
    )


def cleanup_candidate(db: Session, candidate_id: int, *, now: datetime) -> bool:
    hint = db.query(ResumeReplacement).filter(
        ResumeReplacement.new_resume_id == candidate_id,
    ).first()
    relation = None
    if hint is not None:
        relation, _, graph = lock_replacement_graph(
            db, int(hint.id), skip_locked=True,
        )
        candidate = graph.get(candidate_id) if isinstance(graph, dict) else None
    else:
        candidate = (
            db.query(Resume).filter(Resume.id == candidate_id)
            .with_for_update(skip_locked=True).first()
        )
    if candidate is None or not _is_due(candidate, now):
        return False
    if relation is not None:
        relation.lifecycle_status = "closed"
        relation.active_old_resume_id = None
        relation.candidate_cleaned_at = now
        relation.closed_reason = relation.closed_reason or "candidate_expired"
    candidate.deleted_at = now
    candidate.delist_reason = "candidate_expired"
    increment_resume_version(candidate)
    mark_resume_media_delete_pending(db, candidate.id, include_pending=True)
    ensure_target_cleanup_task(db, "resume", candidate.id, reason="candidate_expired")
    db.flush()
    return True


def process_due_candidates(
    db: Session, *, now: datetime | None = None, batch_size: int = BATCH_SIZE,
    max_runtime_seconds: int | None = MAX_RUNTIME_SECONDS, lease=None,
    continuation: Callable[[], None] | None = None,
) -> dict[str, int | bool]:
    moment = now or _utcnow()
    started = time.monotonic()
    stats: dict[str, int | bool] = {
        "cleaned": 0, "conflicts": 0, "batches": 0,
        "continuation_scheduled": False,
    }
    cursor_expiry = None
    cursor_id = 0
    while True:
        if max_runtime_seconds is not None and time.monotonic() - started >= max_runtime_seconds:
            if continuation is None:
                from app.tasks.scheduler import schedule_resume_candidate_continuation
                continuation = schedule_resume_candidate_continuation
            continuation()
            stats["continuation_scheduled"] = True
            break
        query = db.query(Resume.id, Resume.candidate_expires_at).filter(
            Resume.audit_status.in_(("pending", "rejected")),
            Resume.activated_at.is_(None), Resume.expires_at.is_(None),
            Resume.candidate_expires_at.isnot(None),
            Resume.candidate_expires_at <= moment, Resume.deleted_at.is_(None),
        )
        if cursor_expiry is not None:
            query = query.filter(or_(
                Resume.candidate_expires_at > cursor_expiry,
                and_(Resume.candidate_expires_at == cursor_expiry, Resume.id > cursor_id),
            ))
        rows = query.order_by(Resume.candidate_expires_at, Resume.id).limit(batch_size).all()
        if not rows:
            break
        stats["batches"] = int(stats["batches"]) + 1
        for candidate_id, expiry in rows:
            try:
                if cleanup_candidate(db, int(candidate_id), now=moment):
                    db.commit()
                    stats["cleaned"] = int(stats["cleaned"]) + 1
                else:
                    db.rollback()
                    stats["conflicts"] = int(stats["conflicts"]) + 1
            except Exception:
                db.rollback()
                stats["conflicts"] = int(stats["conflicts"]) + 1
                logger.exception("resume candidate cleanup failed: candidate_id={}", candidate_id)
            cursor_expiry, cursor_id = expiry, int(candidate_id)
        if lease is not None and not lease.renew():
            logger.error("resume candidate cleanup lost distributed lease")
            break
        if len(rows) < batch_size:
            break
    return stats


def run() -> None:
    from app.config import settings
    if not settings.resume_candidate_cleanup_enabled:
        log_event("resume_candidate_cleanup_disabled")
        return
    with renewable_task_lock("resume_candidate_cleanup", ttl=1200) as lease:
        if not lease:
            return
        with SessionLocal() as db:
            stats = process_due_candidates(db, now=_utcnow(), lease=lease)
            log_event("resume_candidate_cleanup_summary", **stats)
