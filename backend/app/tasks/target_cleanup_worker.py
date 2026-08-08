"""Consume durable target cleanup tasks."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from app.db import SessionLocal
from app.models import TargetCleanupTask
from app.services.target_cleanup_service import process_cleanup_task
from app.tasks.common import log_event, task_lock


def run(limit: int = 100) -> None:
    owner = f"target-cleanup-{uuid.uuid4()}"
    with task_lock("target_cleanup_worker", ttl=300) as acquired:
        if not acquired:
            return
        with SessionLocal() as db:
            now = datetime.utcnow()
            rows = db.query(TargetCleanupTask).filter(
                TargetCleanupTask.status.in_(("pending", "retry_wait", "processing")),
                (TargetCleanupTask.next_attempt_at.is_(None))
                | (TargetCleanupTask.next_attempt_at <= now),
                (TargetCleanupTask.lease_expires_at.is_(None))
                | (TargetCleanupTask.lease_expires_at <= now),
            ).order_by(TargetCleanupTask.id).with_for_update(skip_locked=True).limit(limit).all()
            ids = []
            for row in rows:
                row.lease_owner = owner
                row.lease_expires_at = now + timedelta(minutes=4)
                ids.append(row.id)
            db.commit()

        succeeded = 0
        for task_id in ids:
            with SessionLocal() as db:
                succeeded += int(process_cleanup_task(db, task_id))
        log_event("target_cleanup_worker", scanned=len(ids), succeeded=succeeded)
