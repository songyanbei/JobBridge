"""Shared locking and lifecycle helpers for resume mutations."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.exceptions import BusinessException
from app.models import Resume, ResumeReplacement


def utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def increment_resume_version(resume: Resume) -> None:
    resume.version = int(resume.version or 0) + 1
    if hasattr(resume, "aggregate_version"):
        resume.aggregate_version = int(getattr(resume, "aggregate_version", None) or resume.version - 1 or 0) + 1


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


def reject_if_replacement_in_progress(db: Session, resume_id: int) -> None:
    relation = db.query(ResumeReplacement).filter(
        ResumeReplacement.active_old_resume_id == resume_id,
    ).with_for_update().first()
    if relation is not None:
        raise BusinessException(40904, "replacement_in_progress")


def close_active_replacement(db: Session, resume_id: int, *, reason: str) -> Resume:
    from app.services.job_media_service import mark_resume_media_delete_pending
    from app.services.resume_replacement_lock_service import (
        current_active_replacement_hint, lock_replacement_graph,
    )
    from app.services.target_cleanup_service import ensure_target_cleanup_task

    hint = current_active_replacement_hint(db, resume_id)
    if hint is None:
        locked = lock_resume(db, resume_id)
        hint = current_active_replacement_hint(db, resume_id)
        if hint is None:
            return locked
    relation, _, graph = lock_replacement_graph(db, hint.id, hint=hint)
    if relation is None or not isinstance(graph, dict):
        raise BusinessException(40904, "replacement_graph_incomplete")
    old = graph.get(resume_id)
    candidate = graph.get(relation.new_resume_id)
    if old is None or candidate is None:
        raise BusinessException(40904, "replacement_graph_incomplete")
    if relation.active_old_resume_id == resume_id:
        now = utc_now_naive()
        relation.lifecycle_status = "closed"
        relation.closed_reason = reason
        relation.active_old_resume_id = None
        relation.candidate_cleaned_at = now
        if candidate is not None and candidate.activated_at is None and candidate.deleted_at is None:
            candidate.deleted_at = now
            candidate.delist_reason = "candidate_cancelled"
            increment_resume_version(candidate)
            mark_resume_media_delete_pending(db, candidate.id)
            ensure_target_cleanup_task(db, "resume", candidate.id, reason="candidate_cancelled")
    return old


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
