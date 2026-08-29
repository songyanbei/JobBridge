"""High-frequency, lock-safe expiry of active Resume rows."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable

from loguru import logger
from sqlalchemy import text

from app.db import SessionLocal
from app.services.target_cleanup_service import ensure_target_cleanup_task
from app.tasks.common import log_event, renewable_task_lock

BATCH_SIZE = 500
MAX_RUNTIME_SECONDS = 8 * 60


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _schedule_continuation() -> None:
    from app.tasks.scheduler import schedule_resume_expiry_continuation
    schedule_resume_expiry_continuation()


def expire_locked_batch(db, *, now: datetime, batch_size: int = BATCH_SIZE) -> list[int]:
    rows = db.execute(text(
        "SELECT id FROM `resume` WHERE audit_status='passed' "
        "AND activated_at IS NOT NULL AND candidate_expires_at IS NULL "
        "AND expires_at IS NOT NULL AND expires_at <= :now "
        "AND deleted_at IS NULL AND delist_reason IS NULL "
        "ORDER BY expires_at,id LIMIT :batch_size FOR UPDATE SKIP LOCKED"
    ), {"now": now, "batch_size": int(batch_size)}).fetchall()
    expired: list[int] = []
    for row in rows:
        resume_id = int(row[0])
        result = db.execute(text(
            "UPDATE `resume` SET delist_reason='expired',deleted_at=:now,"
            "version=version+1 WHERE id=:resume_id AND audit_status='passed' "
            "AND activated_at IS NOT NULL AND candidate_expires_at IS NULL "
            "AND expires_at IS NOT NULL AND expires_at <= :now "
            "AND deleted_at IS NULL AND delist_reason IS NULL"
        ), {"resume_id": resume_id, "now": now})
        if int(result.rowcount or 0) != 1:
            continue
        ensure_target_cleanup_task(db, "resume", resume_id, reason="expired")
        expired.append(resume_id)
    db.commit()
    return expired


def process_expired_resumes(
    db, *, now: datetime | None = None, batch_size: int = BATCH_SIZE,
    max_runtime_seconds: int | None = MAX_RUNTIME_SECONDS, lease=None,
    continuation: Callable[[], None] | None = None,
) -> dict[str, int | bool]:
    # One UTC-naive instant defines the whole invocation, including continuations.
    moment = now or _utcnow()
    started = time.monotonic()
    stats: dict[str, int | bool] = {
        "processed": 0, "batches": 0, "continuation_scheduled": False,
    }
    while True:
        if max_runtime_seconds is not None and time.monotonic() - started >= max_runtime_seconds:
            (continuation or _schedule_continuation)()
            stats["continuation_scheduled"] = True
            break
        ids = expire_locked_batch(db, now=moment, batch_size=batch_size)
        if not ids:
            break
        stats["processed"] = int(stats["processed"]) + len(ids)
        stats["batches"] = int(stats["batches"]) + 1
        if lease is not None and not lease.renew():
            logger.error("resume expiry cleanup lost distributed lease")
            break
    return stats


def run() -> None:
    from app.config import settings
    if not settings.resume_expiry_cleanup_enabled:
        log_event("resume_expiry_cleanup_disabled")
        return
    with renewable_task_lock("resume_expiry_cleanup", ttl=1200) as lease:
        if not lease:
            return
        with SessionLocal() as db:
            moment = _utcnow()
            stats = process_expired_resumes(db, now=moment, lease=lease)
            log_event("resume_expiry_cleanup_summary", **stats)
