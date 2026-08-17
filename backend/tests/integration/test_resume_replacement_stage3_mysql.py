"""Small MySQL proof: concurrent candidate creation has one active relation."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.config import settings
from app.core.exceptions import BusinessException
from app.db import SessionLocal
from app.models import Resume, ResumeReplacement, User
from app.services.resume_replace_service import create_replacement_candidate

pytestmark = pytest.mark.integration


def test_concurrent_resume_updates_create_one_active_relation(monkeypatch):
    monkeypatch.setattr(settings, "resume_replacement_enabled", True)
    owner = f"resume-replace-{uuid4().hex}"
    setup = SessionLocal()
    old_id = None
    try:
        setup.add(User(external_userid=owner, role="worker"))
        setup.flush()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        old = Resume(
            owner_userid=owner, expected_cities=["苏州市"],
            expected_job_categories=["电子厂"], salary_expect_floor_monthly=5000,
            gender="男", age=30, raw_text="old", audit_status="passed",
            activated_at=now, expires_at=now + timedelta(days=30), version=1,
        )
        setup.add(old)
        setup.commit()
        old_id = old.id
        gate = Barrier(2)

        def attempt(index):
            db = SessionLocal()
            try:
                gate.wait(timeout=5)
                relation, _ = create_replacement_candidate(
                    db, owner_userid=owner, target_resume_id=old_id,
                    expected_version=1, operation_id=str(uuid4()),
                    source_msg_id=f"message-{index}-{uuid4()}",
                    complete_data={
                        "expected_cities": ["昆山市"],
                        "expected_job_categories": ["物流仓储"],
                        "salary_expect_floor_monthly": 6000, "gender": "男", "age": 30,
                    },
                    raw_text=f"new-{index}", media_ids=[],
                    audit_result=SimpleNamespace(status="pending", reason=""),
                )
                db.commit()
                return ("created", relation.id)
            except BusinessException as exc:
                db.rollback()
                return ("blocked", str(exc))
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, (1, 2)))
        assert sorted(result[0] for result in results) == ["blocked", "created"]

        verify = SessionLocal()
        try:
            assert verify.query(ResumeReplacement).filter(
                ResumeReplacement.owner_userid == owner,
                ResumeReplacement.active_old_resume_id == old_id,
            ).count() == 1
        finally:
            verify.close()
    finally:
        setup.rollback()
        setup.close()
        cleanup = SessionLocal()
        try:
            cleanup.query(ResumeReplacement).filter(
                ResumeReplacement.owner_userid == owner,
            ).delete(synchronize_session=False)
            cleanup.query(Resume).filter(Resume.owner_userid == owner).delete(
                synchronize_session=False,
            )
            cleanup.query(User).filter(User.external_userid == owner).delete(
                synchronize_session=False,
            )
            cleanup.commit()
        finally:
            cleanup.close()
