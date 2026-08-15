"""Durable target cleanup checkpoints across a real Redis failure."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.core.redis_client import (
    RECOMMENDATION_SESSION_REVOCATION_FENCE_KEY,
    RECOMMENDATION_SESSION_TARGET_INDEX_PREFIX,
    get_redis,
)
from app.db import SessionLocal
from app.models import TargetCleanupTask
from app.services.target_cleanup_service import process_cleanup_task


pytestmark = pytest.mark.integration


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def test_redis_failure_preserves_prior_checkpoints_and_retry_completes():
    target_id = int(uuid4().int % 9_000_000_000) + 20_000_000_000
    target_index = (
        f"{RECOMMENDATION_SESSION_TARGET_INDEX_PREFIX}job:{target_id}"
    )
    redis = get_redis()
    task_id = None
    redis.delete(target_index)
    redis.srem(RECOMMENDATION_SESSION_REVOCATION_FENCE_KEY, target_index)
    redis.set(target_index, "wrong-type")

    try:
        first_owner = f"checkpoint-first-{uuid4()}"
        with SessionLocal() as setup_db:
            task = TargetCleanupTask(
                operation_id=str(uuid4()),
                target_type="job",
                target_id=target_id,
                reason="expired",
                reason_history=["expired"],
                status="processing",
                attempt_count=1,
                lease_owner=first_owner,
                lease_expires_at=_now() + timedelta(minutes=4),
            )
            setup_db.add(task)
            setup_db.commit()
            task_id = int(task.id)

        with SessionLocal() as process_db:
            assert not process_cleanup_task(process_db, task_id, first_owner)

        with SessionLocal() as verify_db:
            failed = verify_db.get(TargetCleanupTask, task_id)
            assert failed.status == "retry_wait"
            assert failed.attempt_count == 1
            assert failed.db_redacted_at is not None
            assert failed.conversation_redacted_at is not None
            assert failed.session_invalidated_at is None
            assert "SESSION_INDEX_WRONGTYPE" in failed.last_error
            db_checkpoint = datetime(2000, 1, 1, 0, 0, 1)
            conversation_checkpoint = datetime(2000, 1, 1, 0, 0, 2)
            second_owner = f"checkpoint-second-{uuid4()}"
            failed.db_redacted_at = db_checkpoint
            failed.conversation_redacted_at = conversation_checkpoint
            failed.status = "processing"
            failed.attempt_count = 2
            failed.next_attempt_at = None
            failed.lease_owner = second_owner
            failed.lease_expires_at = _now() + timedelta(minutes=4)
            verify_db.commit()

        redis.delete(target_index)
        with SessionLocal() as process_db:
            assert process_cleanup_task(process_db, task_id, second_owner)

        with SessionLocal() as verify_db:
            completed = verify_db.get(TargetCleanupTask, task_id)
            assert completed.status == "succeeded"
            assert completed.attempt_count == 2
            assert completed.db_redacted_at == db_checkpoint
            assert completed.conversation_redacted_at == conversation_checkpoint
            assert completed.session_invalidated_at is not None
            assert completed.lease_owner is None
            assert completed.lease_expires_at is None
    finally:
        redis.delete(target_index)
        redis.srem(RECOMMENDATION_SESSION_REVOCATION_FENCE_KEY, target_index)
        if task_id is not None:
            with SessionLocal() as cleanup_db:
                cleanup_db.query(TargetCleanupTask).filter(
                    TargetCleanupTask.id == task_id,
                ).delete(synchronize_session=False)
                cleanup_db.commit()
