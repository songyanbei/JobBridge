"""Target cleanup lease fencing against real MySQL current reads."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.db import SessionLocal
from app.models import TargetCleanupTask
from app.services.target_cleanup_service import checkpoint_cleanup_task


pytestmark = pytest.mark.integration


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _task(*, owner: str, expires_at: datetime):
    return TargetCleanupTask(
        operation_id=str(uuid4()),
        target_type="job",
        target_id=int(uuid4().int % 9_000_000_000) + 1_000_000_000,
        reason="expired",
        reason_history=["expired"],
        status="processing",
        attempt_count=1,
        lease_owner=owner,
        lease_expires_at=expires_at,
    )


def test_old_owner_and_expired_lease_cannot_write_checkpoints():
    setup_db = SessionLocal()
    worker_db = SessionLocal()
    stale_worker_db = SessionLocal()
    task_ids = []
    try:
        now = _now()
        valid = _task(
            owner="worker-1", expires_at=now + timedelta(minutes=1),
        )
        stolen = _task(
            owner="worker-1", expires_at=now + timedelta(minutes=1),
        )
        expired = _task(
            owner="worker-1", expires_at=now - timedelta(seconds=1),
        )
        setup_db.add_all([valid, stolen, expired])
        setup_db.commit()
        task_ids = [int(valid.id), int(stolen.id), int(expired.id)]

        stale_snapshot = stale_worker_db.get(TargetCleanupTask, task_ids[1])
        assert stale_snapshot.lease_owner == "worker-1"
        setup_db.query(TargetCleanupTask).filter(
            TargetCleanupTask.id == task_ids[1],
        ).update(
            {"lease_owner": "worker-2"},
            synchronize_session=False,
        )
        setup_db.commit()

        assert checkpoint_cleanup_task(
            worker_db,
            task_ids[0],
            "worker-1",
            "db_redacted_at",
            now,
            delivery_ids=["delivery-valid"],
        )
        assert not checkpoint_cleanup_task(
            stale_worker_db,
            task_ids[1],
            "worker-1",
            "db_redacted_at",
            now,
            delivery_ids=["delivery-stale-owner"],
        )
        assert not checkpoint_cleanup_task(
            worker_db,
            task_ids[2],
            "worker-1",
            "db_redacted_at",
            now,
            delivery_ids=["delivery-expired"],
        )

        with SessionLocal() as verify_db:
            saved_valid = verify_db.get(TargetCleanupTask, task_ids[0])
            saved_stolen = verify_db.get(TargetCleanupTask, task_ids[1])
            saved_expired = verify_db.get(TargetCleanupTask, task_ids[2])
            assert saved_valid.db_redacted_at is not None
            assert saved_valid.delivery_ids == ["delivery-valid"]
            assert saved_stolen.db_redacted_at is None
            assert saved_stolen.delivery_ids is None
            assert saved_expired.db_redacted_at is None
            assert saved_expired.delivery_ids is None
    finally:
        stale_worker_db.rollback()
        stale_worker_db.close()
        worker_db.rollback()
        worker_db.close()
        setup_db.rollback()
        if task_ids:
            setup_db.query(TargetCleanupTask).filter(
                TargetCleanupTask.id.in_(task_ids),
            ).delete(synchronize_session=False)
            setup_db.commit()
        setup_db.close()
