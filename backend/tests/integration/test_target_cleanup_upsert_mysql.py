"""Concurrent target cleanup task creation on real MySQL."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.db import SessionLocal
from app.models import Job, TargetCleanupTask, User
from app.services import target_cleanup_service
from app.services.target_cleanup_service import upsert_job_cleanup_task


pytestmark = pytest.mark.integration


def test_concurrent_target_cleanup_upsert_creates_one_complete_task():
    setup_db = SessionLocal()
    owner = f"cleanup-upsert-{uuid4().hex}"
    job_id = None
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        setup_db.add(User(external_userid=owner, role="factory"))
        setup_db.commit()
        job = Job(
            owner_userid=owner,
            city="N01",
            job_category="N01",
            salary_floor_monthly=5000,
            pay_type=Job.__table__.c.pay_type.type.enums[0],
            headcount=1,
            raw_text="target cleanup upsert",
            audit_status="passed",
            activated_at=now,
            expires_at=now + timedelta(days=30),
        )
        setup_db.add(job)
        setup_db.commit()
        job_id = int(job.id)

        barrier = threading.Barrier(2)

        def _upsert(reason: str) -> tuple[int, bool]:
            db = SessionLocal()
            try:
                barrier.wait(timeout=10)
                task, created = upsert_job_cleanup_task(
                    db,
                    job_id,
                    reason=reason,
                    operation_id=str(uuid4()),
                )
                task_id = int(task.id)
                db.commit()
                return task_id, created
            finally:
                db.rollback()
                db.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(_upsert, ("expired", "manual_delete")))

        assert len({task_id for task_id, _ in results}) == 1
        assert sorted(created for _, created in results) == [False, True]
        setup_db.expire_all()
        tasks = setup_db.query(TargetCleanupTask).filter(
            TargetCleanupTask.target_type == "job",
            TargetCleanupTask.target_id == job_id,
        ).all()
        assert len(tasks) == 1
        assert set(tasks[0].reason_history) == {"expired", "manual_delete"}
    finally:
        setup_db.rollback()
        if job_id is not None:
            setup_db.query(TargetCleanupTask).filter(
                TargetCleanupTask.target_type == "job",
                TargetCleanupTask.target_id == job_id,
            ).delete(synchronize_session=False)
            setup_db.query(Job).filter(Job.id == job_id).delete(
                synchronize_session=False,
            )
        setup_db.query(User).filter(User.external_userid == owner).delete(
            synchronize_session=False,
        )
        setup_db.commit()
        setup_db.close()


def test_unique_race_recovers_without_rolling_back_outer_transaction(monkeypatch):
    setup_db = SessionLocal()
    caller_db = SessionLocal()
    owner = f"cleanup-savepoint-{uuid4().hex}"
    job_id = None
    winner_operation_id = str(uuid4())
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        setup_db.add(User(external_userid=owner, role="factory"))
        setup_db.commit()
        job = Job(
            owner_userid=owner,
            city="before-savepoint",
            job_category="N01",
            salary_floor_monthly=5000,
            pay_type=Job.__table__.c.pay_type.type.enums[0],
            headcount=1,
            raw_text="target cleanup savepoint",
            audit_status="passed",
            activated_at=now,
            expires_at=now + timedelta(days=30),
        )
        setup_db.add(job)
        setup_db.commit()
        job_id = int(job.id)

        caller_job = caller_db.query(Job).filter(Job.id == job_id).one()
        caller_job.city = "outer-write-survived"
        original_lock = target_cleanup_service._lock_job_cleanup_task
        injected = False

        def _inject_unique_winner(db, locked_job_id):
            nonlocal injected
            if not injected:
                injected = True
                winner_db = SessionLocal()
                try:
                    winner_db.add(TargetCleanupTask(
                        operation_id=winner_operation_id,
                        target_type="job",
                        target_id=locked_job_id,
                        reason="expired",
                        reason_history=["expired"],
                        status="pending",
                    ))
                    winner_db.commit()
                finally:
                    winner_db.close()
                return None
            return original_lock(db, locked_job_id)

        monkeypatch.setattr(
            target_cleanup_service, "_lock_job_cleanup_task", _inject_unique_winner,
        )
        task, created = upsert_job_cleanup_task(
            caller_db,
            job_id,
            reason="manual_delete",
            operation_id=str(uuid4()),
        )
        assert created is False
        assert task.operation_id == winner_operation_id
        caller_db.commit()

        setup_db.expire_all()
        saved_job = setup_db.query(Job).filter(Job.id == job_id).one()
        saved_task = setup_db.query(TargetCleanupTask).filter(
            TargetCleanupTask.target_type == "job",
            TargetCleanupTask.target_id == job_id,
        ).one()
        assert saved_job.city == "outer-write-survived"
        assert set(saved_task.reason_history) == {"expired", "manual_delete"}
    finally:
        caller_db.rollback()
        caller_db.close()
        setup_db.rollback()
        if job_id is not None:
            setup_db.query(TargetCleanupTask).filter(
                TargetCleanupTask.target_type == "job",
                TargetCleanupTask.target_id == job_id,
            ).delete(synchronize_session=False)
            setup_db.query(Job).filter(Job.id == job_id).delete(
                synchronize_session=False,
            )
        setup_db.query(User).filter(User.external_userid == owner).delete(
            synchronize_session=False,
        )
        setup_db.commit()
        setup_db.close()
