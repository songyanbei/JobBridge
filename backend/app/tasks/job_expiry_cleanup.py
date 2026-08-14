"""High-frequency, ordered and lock-safe Job expiry processing."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable

from loguru import logger
from sqlalchemy import text

from app.db import SessionLocal
from app.services.target_cleanup_service import ensure_job_cleanup_task
from app.tasks.common import log_event, renewable_task_lock

BATCH_SIZE = 500
MAX_RUNTIME_SECONDS = 8 * 60


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _schedule_continuation() -> None:
    from app.tasks.scheduler import schedule_job_expiry_continuation
    schedule_job_expiry_continuation()


def expire_locked_batch(db, *, now: datetime, batch_size: int = BATCH_SIZE) -> list[int]:
    """Expire one locked batch; only successful conditional updates produce cleanup work."""
    rows = db.execute(text(
        "SELECT id FROM `job` "
        "WHERE expires_at <= :now AND deleted_at IS NULL AND delist_reason IS NULL "
        "ORDER BY expires_at ASC, id ASC "
        "LIMIT :batch_size FOR UPDATE SKIP LOCKED"
    ), {"now": now, "batch_size": int(batch_size)}).fetchall()
    expired_ids: list[int] = []
    for row in rows:
        job_id = int(row[0])
        result = db.execute(text(
            "UPDATE `job` SET delist_reason='expired', deleted_at=:now, "
            "version=version+1 WHERE id=:job_id AND expires_at <= :now "
            "AND deleted_at IS NULL AND delist_reason IS NULL"
        ), {"job_id": job_id, "now": now})
        if int(result.rowcount or 0) != 1:
            continue
        expired_ids.append(job_id)
        ensure_job_cleanup_task(db, job_id, reason="expired")
    db.commit()
    return expired_ids


def process_expired_jobs(
    db,
    *,
    batch_size: int = BATCH_SIZE,
    max_runtime_seconds: int | None = MAX_RUNTIME_SECONDS,
    lease=None,
    continuation: Callable[[], None] | None = None,
) -> dict[str, int | bool]:
    started = time.monotonic()
    stats: dict[str, int | bool] = {
        "processed": 0,
        "batches": 0,
        "continuation_scheduled": False,
    }
    while True:
        if (
            max_runtime_seconds is not None
            and time.monotonic() - started >= max_runtime_seconds
        ):
            (continuation or _schedule_continuation)()
            stats["continuation_scheduled"] = True
            break
        expired_ids = expire_locked_batch(db, now=_utcnow(), batch_size=batch_size)
        if not expired_ids:
            break
        stats["processed"] = int(stats["processed"]) + len(expired_ids)
        stats["batches"] = int(stats["batches"]) + 1
        if lease is not None and not lease.renew():
            logger.error("job expiry cleanup lost distributed lease")
            break
    return stats


def run() -> None:
    from app.config import settings
    if not settings.job_expiry_cleanup_enabled:
        log_event("job_expiry_cleanup_disabled")
        return
    with renewable_task_lock("job_expiry_cleanup", ttl=1200) as lease:
        if not lease:
            return
        with SessionLocal() as db:
            now = _utcnow()
            due_count, oldest_due = db.execute(text(
                "SELECT COUNT(*), MIN(expires_at) FROM `job` "
                "WHERE expires_at <= :now AND deleted_at IS NULL AND delist_reason IS NULL"
            ), {"now": now}).one()
            started = time.monotonic()
            stats = process_expired_jobs(db, lease=lease)
            log_event(
                "job_expiry_cleanup_summary",
                **stats,
                elapsed_seconds=round(time.monotonic() - started, 3),
                job_expiry_due_count=int(due_count or 0),
                job_expiry_oldest_lag_seconds=(
                    max(0, int((now - oldest_due).total_seconds())) if oldest_due else 0
                ),
                job_expiry_batches=stats["batches"],
                job_expiry_continuation_scheduled=stats["continuation_scheduled"],
            )
