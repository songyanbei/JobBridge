"""Consume durable target cleanup tasks."""
from __future__ import annotations

import uuid
from datetime import datetime

from app.db import SessionLocal
from app.services.target_cleanup_service import (
    claim_cleanup_tasks,
    process_cleanup_task,
)
from app.tasks.common import log_event, task_lock


def run(limit: int = 100) -> None:
    owner = f"target-cleanup-{uuid.uuid4()}"
    with task_lock("target_cleanup_worker", ttl=300) as acquired:
        if not acquired:
            return
        with SessionLocal() as db:
            ids = claim_cleanup_tasks(db, owner, datetime.utcnow(), limit)

        succeeded = 0
        for task_id in ids:
            with SessionLocal() as db:
                succeeded += int(process_cleanup_task(db, task_id, owner))
        log_event("target_cleanup_worker", scanned=len(ids), succeeded=succeeded)
