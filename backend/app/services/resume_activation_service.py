"""The only activation primitive for first-publish resumes."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models import Resume
from app.services.lifecycle_config_service import get_resume_ttl_days
from app.services.resume_mutation_service import (
    increment_resume_version,
    to_utc_naive,
    utc_now_naive,
)


def activate_resume(
    db: Session,
    resume: Resume,
    *,
    now: datetime | None = None,
) -> Resume:
    """Activate once, using one UTC-naive instant for both lifecycle fields.

    The compatibility double-write is unconditional: feature flags deliberately
    do not participate in this function.
    """
    if (
        resume.audit_status == "passed"
        and resume.activated_at is not None
        and resume.expires_at is not None
        and resume.candidate_expires_at is None
    ):
        return resume

    activated_at = to_utc_naive(now) if now is not None else utc_now_naive()
    resume.audit_status = "passed"
    resume.activated_at = activated_at
    resume.expires_at = activated_at + timedelta(days=get_resume_ttl_days(db))
    resume.candidate_expires_at = None
    resume.delist_reason = None
    increment_resume_version(resume)
    db.flush()
    return resume
