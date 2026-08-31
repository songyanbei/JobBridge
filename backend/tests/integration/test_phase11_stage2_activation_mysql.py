"""Minimal real-MySQL transaction gate for the stage-2 activation primitive."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.db import SessionLocal
from app.models import AuditLog, Resume, User
from app.services import audit_workbench_service
from app.services import resume_mutation_service
from app.services.resume_mutation_service import lock_resume


pytestmark = pytest.mark.integration
# Keep the fixture relative to the test runtime while freezing the service
# clock below, so this integration test remains reproducible after the date
# moves past the original historical fixture.
NOW = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0) + timedelta(days=1)


@pytest.fixture(autouse=True)
def _freeze_audit_clock(monkeypatch):
    # _pass_resume imports the clock from resume_mutation_service at call time.
    monkeypatch.setattr(resume_mutation_service, "utc_now_naive", lambda: NOW)


def _candidate(owner: str) -> Resume:
    return Resume(
        owner_userid=owner,
        expected_cities=["苏州"],
        expected_job_categories=["电子厂"],
        salary_expect_floor_monthly=5000,
        gender="男",
        age=30,
        raw_text="stage2 mysql candidate",
        audit_status="pending",
        candidate_expires_at=NOW + timedelta(days=7),
        version=1,
    )


def test_activation_row_lock_commit_and_failure_rollback(monkeypatch, request):
    owner = f"phase11-s2-{uuid4().hex}"
    setup = SessionLocal()
    try:
        setup.add(User(external_userid=owner, role="worker"))
        setup.flush()
        committed = _candidate(owner)
        rollback = _candidate(owner)
        setup.add_all([committed, rollback])
        setup.commit()
        committed_id, rollback_id = committed.id, rollback.id
    finally:
        setup.close()

    def cleanup_owned_rows() -> None:
        cleanup = SessionLocal()
        try:
            target_ids = [str(committed_id), str(rollback_id)]
            cleanup.query(AuditLog).filter(
                AuditLog.target_type == "resume",
                AuditLog.target_id.in_(target_ids),
            ).delete(synchronize_session=False)
            cleanup.query(Resume).filter(
                Resume.id.in_([committed_id, rollback_id]),
                Resume.owner_userid == owner,
            ).delete(synchronize_session=False)
            cleanup.query(User).filter(
                User.external_userid == owner,
            ).delete(synchronize_session=False)
            cleanup.commit()
        finally:
            cleanup.close()

    # Register immediately after the fixture rows commit, so assertion and
    # worker failures cannot leak a passed or pending Resume into later
    # LIMIT/SKIP LOCKED acceptance units sharing this isolated schema.
    request.addfinalizer(cleanup_owned_rows)

    locker = SessionLocal()
    contender = SessionLocal()
    try:
        lock_resume(locker, committed_id)
        contender.execute(text("SET SESSION innodb_lock_wait_timeout=1"))
        with pytest.raises(OperationalError) as exc:
            lock_resume(contender, committed_id)
        assert exc.value.orig.args[0] == 1205
        contender.rollback()
        contender.execute(text(
            "SET SESSION innodb_lock_wait_timeout=@@GLOBAL.innodb_lock_wait_timeout"
        ))
        contender.commit()
    finally:
        locker.rollback()
        locker.close()
        contender.close()

    passing = SessionLocal()
    try:
        audit_workbench_service._pass_resume(passing, committed_id, 1, "admin-1")
    finally:
        passing.close()

    verify = SessionLocal()
    try:
        row = verify.get(Resume, committed_id)
        assert row.audit_status == "passed"
        assert row.activated_at is not None
        assert row.expires_at == row.activated_at + timedelta(days=30)
        assert row.candidate_expires_at is None
        assert row.version == 2
    finally:
        verify.close()

    failing = SessionLocal()
    monkeypatch.setattr(
        audit_workbench_service,
        "write_admin_log",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("audit failed")),
    )
    try:
        with pytest.raises(RuntimeError, match="audit failed"):
            audit_workbench_service._pass_resume(failing, rollback_id, 1, "admin-1")
        failing.rollback()
    finally:
        failing.close()

    verify = SessionLocal()
    try:
        row = verify.get(Resume, rollback_id)
        assert row.audit_status == "pending"
        assert row.activated_at is None
        assert row.expires_at is None
        assert row.candidate_expires_at == NOW + timedelta(days=7)
        assert row.version == 1
    finally:
        verify.close()
