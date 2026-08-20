"""Minimal real-MySQL concurrency gate for phase-6 cleanup administration."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import hashlib
from threading import Barrier, Lock
from uuid import uuid4

import pytest

from app.db import SessionLocal
from app.models import (
    AdminUser, AuditLog, Resume, ResumeMediaIsolationIssue, TargetCleanupTask, User,
)
from app.services import cleanup_admin_service


pytestmark = pytest.mark.integration


def test_redrive_rate_limit_is_atomic_for_three_concurrent_batches():
    operator = f"phase6-{uuid4().hex[:20]}"
    operation_ids = [str(uuid4()) for _ in range(3)]
    setup = SessionLocal()
    try:
        setup.add(AdminUser(
            username=operator, password_hash="phase6-test-only", role="super_admin",
            password_changed=1, enabled=1,
        ))
        rows = [
            TargetCleanupTask(
                operation_id=operation_id, target_type="resume", target_id=10_000_000 + index,
                reason="expired", status="dead_letter", attempt_count=10,
            )
            for index, operation_id in enumerate(operation_ids)
        ]
        setup.add_all(rows)
        setup.commit()
        task_ids = [int(row.id) for row in rows]
    finally:
        setup.close()

    # Start three real service calls together. The durable AdminUser row lock
    # must serialize their complete check -> audit -> commit units.
    ready = Barrier(3)
    results: list[str] = []
    result_lock = Lock()

    def redrive(task_id: int):
        db = SessionLocal()
        try:
            ready.wait(timeout=10)
            cleanup_admin_service.redrive_dead_letters(
                db, kind="target", ids=[task_id], reason="concurrency gate",
                operator=operator,
            )
            outcome = "queued"
        except Exception as exc:  # asserted by the parent thread
            db.rollback()
            outcome = getattr(exc, "message", type(exc).__name__)
        finally:
            db.close()
        with result_lock:
            results.append(outcome)

    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(redrive, task_ids))
        assert results.count("queued") <= cleanup_admin_service.MAX_REDRIVE_BATCHES_PER_MINUTE
        assert results.count("cleanup_redrive_rate_limited") >= 1
    finally:
        cleanup = SessionLocal()
        try:
            cleanup.query(AuditLog).filter(AuditLog.operator == operator).delete(
                synchronize_session=False,
            )
            cleanup.query(TargetCleanupTask).filter(
                TargetCleanupTask.operation_id.in_(operation_ids),
            ).delete(synchronize_session=False)
            cleanup.query(AdminUser).filter(AdminUser.username == operator).delete(
                synchronize_session=False,
            )
            cleanup.commit()
        finally:
            cleanup.close()


def test_media_disposition_has_one_distinct_executor_and_one_atomic_effect():
    owner = f"phase6-worker-{uuid4().hex[:16]}"
    object_key = f"private/resume/{uuid4().hex}.jpg"
    setup = SessionLocal()
    try:
        setup.add(User(external_userid=owner, role="worker"))
        setup.flush()
        resume = Resume(
            owner_userid=owner, expected_cities=["苏州"],
            expected_job_categories=["普工"], salary_expect_floor_monthly=5000,
            gender="男", age=30, accept_long_term=1, accept_short_term=0,
            raw_text="phase6 media", images=[object_key], audit_status="passed",
            activated_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(days=1), version=1,
        )
        setup.add(resume)
        setup.flush()
        issue = ResumeMediaIsolationIssue(
            resume_id=resume.id,
            key_hash=hashlib.sha256(object_key.encode("utf-8")).hexdigest(),
            issue_type="shared_reference", status="open",
        )
        setup.add(issue)
        setup.commit()
        resume_id, issue_id = int(resume.id), int(issue.id)
        cleanup_admin_service.approve_media_issue(
            setup, issue_id=issue_id, disposition="detach_reference",
            reason="verified", operator="admin-a",
        )
    finally:
        setup.close()

    ready = Barrier(2)

    def execute(operator: str) -> str:
        db = SessionLocal()
        try:
            ready.wait(timeout=10)
            cleanup_admin_service.execute_media_issue(
                db, issue_id=issue_id, operator=operator,
            )
            return "resolved"
        except Exception as exc:
            db.rollback()
            return getattr(exc, "message", type(exc).__name__)
        finally:
            db.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(execute, ("admin-b", "admin-c")))
        assert outcomes.count("resolved") == 1
        assert outcomes.count("media_isolation_issue_not_approved") == 1

        verify = SessionLocal()
        try:
            persisted_resume = verify.get(Resume, resume_id)
            persisted_issue = verify.get(ResumeMediaIsolationIssue, issue_id)
            assert persisted_resume.images == [] and persisted_resume.version == 2
            assert persisted_issue.status == "resolved"
            assert persisted_issue.approved_by == "admin-a"
            assert persisted_issue.executed_by in {"admin-b", "admin-c"}
            assert verify.query(AuditLog).filter(
                AuditLog.target_id == f"media-isolation:{issue_id}",
                AuditLog.reason == "media_isolation_executed",
            ).count() == 1
        finally:
            verify.close()
    finally:
        cleanup = SessionLocal()
        try:
            cleanup.query(AuditLog).filter(
                AuditLog.target_id == f"media-isolation:{issue_id}",
            ).delete(synchronize_session=False)
            cleanup.query(ResumeMediaIsolationIssue).filter_by(id=issue_id).delete(
                synchronize_session=False,
            )
            cleanup.query(Resume).filter_by(id=resume_id).delete(synchronize_session=False)
            cleanup.query(User).filter_by(external_userid=owner).delete(synchronize_session=False)
            cleanup.commit()
        finally:
            cleanup.close()
