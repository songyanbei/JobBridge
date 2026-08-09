"""Job replacement integration checks that require a real MySQL database."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.exceptions import BusinessException
from app.db import SessionLocal
from app.models import Job, JobReplacement, User
from app.services.job_replace_service import cancel_candidate


pytestmark = pytest.mark.integration


def test_cancel_reason_limit_prevents_mysql_truncation():
    db = SessionLocal()
    prefix = f"replace-limit-{uuid4().hex}"
    accepted_reason = "r" * 64
    rejected_reason = "r" * 65

    try:
        sql_mode = str(db.execute(text("SELECT @@SESSION.sql_mode")).scalar_one())
        assert "STRICT_TRANS_TABLES" in sql_mode or "STRICT_ALL_TABLES" in sql_mode
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        owner = User(external_userid=prefix, role="factory")
        old_job = Job(
            owner_userid=prefix,
            city="O02",
            job_category="O02",
            salary_floor_monthly=5000,
            pay_type="月薪",
            headcount=1,
            raw_text="old job",
            audit_status="passed",
            activated_at=now,
            expires_at=now + timedelta(days=30),
        )
        candidate = Job(
            owner_userid=prefix,
            city="O02",
            job_category="O02",
            salary_floor_monthly=5000,
            pay_type="月薪",
            headcount=1,
            raw_text="replacement candidate",
            audit_status="pending",
            candidate_expires_at=now + timedelta(days=1),
        )
        db.add(owner)
        db.flush()
        db.add_all([old_job, candidate])
        db.flush()

        relation = JobReplacement(
            operation_id=str(uuid4()),
            source_msg_id=f"msg-{uuid4()}",
            owner_userid=prefix,
            old_job_id=old_job.id,
            new_job_id=candidate.id,
            old_job_version=old_job.version,
            old_expires_at=old_job.expires_at,
            old_business_digest="0" * 64,
            old_business_digest_version=1,
            review_outcome="pending",
            lifecycle_status="awaiting_review",
            active_old_job_id=old_job.id,
        )
        db.add(relation)
        db.flush()

        with pytest.raises(BusinessException, match="replacement_cancel_reason_invalid"):
            cancel_candidate(
                db,
                relation.id,
                operator="integration-reviewer",
                reason=rejected_reason,
            )
        assert relation.lifecycle_status == "awaiting_review"
        assert relation.closed_reason is None
        assert candidate.deleted_at is None

        cancel_candidate(
            db,
            relation.id,
            operator="integration-reviewer",
            reason=accepted_reason,
        )
        db.flush()

        stored_reason = db.execute(
            text("SELECT closed_reason FROM job_replacement WHERE id=:id"),
            {"id": relation.id},
        ).scalar_one()
        assert stored_reason == accepted_reason
    finally:
        db.rollback()
        db.close()
