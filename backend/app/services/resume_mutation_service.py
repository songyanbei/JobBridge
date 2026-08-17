"""Shared locking and lifecycle helpers for resume mutations."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.exceptions import BusinessException
from app.models import Resume


def utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def increment_resume_version(resume: Resume) -> None:
    resume.version = int(resume.version or 0) + 1


def lock_resume(db: Session, resume_id: int) -> Resume:
    resume = (
        db.query(Resume)
        .filter(Resume.id == resume_id)
        .with_for_update()
        .first()
    )
    if resume is None:
        raise BusinessException(40401, "审核对象不存在")
    return resume


def assert_resume_activatable(
    resume: Resume,
    *,
    now: datetime,
    strict: bool | None = None,
) -> None:
    if strict is None:
        from app.config import settings

        strict = settings.resume_lifecycle_v2_enabled
    if resume.audit_status != "pending":
        raise BusinessException(40904, "resume_not_pending_activation")
    if resume.deleted_at is not None or resume.delist_reason is not None:
        raise BusinessException(40904, "candidate_expired")
    if strict and (resume.expires_at is not None or resume.candidate_expires_at is None):
        raise BusinessException(40904, "resume_not_pending_activation")
    if (
        resume.candidate_expires_at is not None
        and to_utc_naive(resume.candidate_expires_at) <= to_utc_naive(now)
    ):
        raise BusinessException(40904, "candidate_expired")


def resume_is_online(
    resume: Resume,
    *,
    now: datetime,
    strict: bool = False,
) -> bool:
    expires_at = resume.expires_at
    return bool(
        resume.audit_status == "passed"
        and resume.deleted_at is None
        and resume.delist_reason is None
        and expires_at is not None
        and to_utc_naive(expires_at) > to_utc_naive(now)
        and (not strict or resume.activated_at is not None)
    )


def online_resume_filters(
    *,
    now: datetime | None = None,
    strict: bool | None = None,
) -> tuple:
    """Canonical SQL predicate shared by every ordinary resume target read."""
    if strict is None:
        from app.config import settings

        strict = settings.resume_lifecycle_v2_enabled
    moment = to_utc_naive(now) if now is not None else utc_now_naive()
    filters = [
        Resume.audit_status == "passed",
        Resume.deleted_at.is_(None),
        Resume.delist_reason.is_(None),
        Resume.expires_at.isnot(None),
        Resume.expires_at > moment,
    ]
    if strict:
        filters.append(Resume.activated_at.isnot(None))
    return tuple(filters)
