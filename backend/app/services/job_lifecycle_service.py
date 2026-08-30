"""Small, transaction-friendly Job lifecycle primitives used by S4 paths.

The legacy admin service remains the compatibility entry point.  These helpers
centralize state predicates and optionally emit the versioned domain event when
the S4 outbox model is present in a mixed-version deployment.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.exceptions import BusinessException
from app.models import Job
from app.services.job_media_service import mark_job_media_delete_pending


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _emit(db: Session, job: Job, event_type: str, *, reason: str | None = None, tombstone: bool = False) -> None:
    """Best-effort bridge to the S4 DomainOutboxEvent model.

    Older workers do not have that model yet; keeping the event in ``Session``
    info lets the new writer flush it after the model migration without making
    legacy lifecycle operations fail.
    """
    aggregate_version = int(
        getattr(job, "aggregate_version", None)
        or getattr(job, "version", None)
        or 1
    )
    payload = {
        "aggregate_type": "job",
        "aggregate_id": int(job.id),
        "aggregate_version": aggregate_version,
        "event_type": event_type,
        "reason": reason,
        "tombstone": bool(tombstone),
    }
    try:
        from app.services.domain_outbox_service import append_domain_event
    except ImportError:
        db.info.setdefault("pending_job_lifecycle_events", []).append(payload)
        return
    append_domain_event(
        db,
        aggregate_type="job",
        aggregate_id=int(job.id),
        aggregate_version=aggregate_version,
        event_type=event_type,
        payload={"reason": reason} if reason else {},
        tombstone=tombstone,
    )


def transition_job(
    db: Session,
    job_id: int,
    *,
    action: str,
    expected_version: int | None = None,
    operator: str | None = None,
    reason: str | None = None,
    ttl_days: int | None = None,
) -> Job:
    """Apply one lifecycle transition under a row lock and return the row.

    ``db.commit`` is deliberately left to the caller so the transition can be
    composed with cleanup tasks, Contact revocation and Action finalization.
    """
    job = db.query(Job).populate_existing().filter(Job.id == job_id).with_for_update().one_or_none()
    if job is None:
        raise BusinessException(40401, "岗位不存在")
    current = int(job.version or 1)
    if expected_version is not None and current != int(expected_version):
        raise BusinessException(40902, "岗位已发生变化，请刷新", {"current_version": current})
    now = utcnow()
    action = str(action).strip().lower()
    if action in {"delist", "manual_delist", "filled"}:
        if job.deleted_at is not None:
            raise BusinessException(40904, "job_deleted")
        lifecycle_reason = "filled" if action == "filled" else (reason or "manual_delist")
        if lifecycle_reason not in {"manual_delist", "filled", "expired", "replaced"}:
            raise BusinessException(40101, "无效的下架原因")
        if job.delist_reason is not None:
            return job
        job.delist_reason = lifecycle_reason
        job.deleted_at = now if lifecycle_reason in {"expired", "replaced"} else job.deleted_at
        job.version = current + 1
        mark_job_media_delete_pending(db, job.id)
        _emit(db, job, "job.expired" if lifecycle_reason == "expired" else "job.delisted", reason=lifecycle_reason, tombstone=True)
    elif action == "expire":
        if job.deleted_at is not None or job.delist_reason is not None:
            return job
        if job.expires_at is None or job.expires_at > now:
            raise BusinessException(40904, "job_not_expired")
        job.delist_reason = "expired"
        job.deleted_at = now
        job.version = current + 1
        mark_job_media_delete_pending(db, job.id)
        _emit(db, job, "job.expired", reason="expired", tombstone=True)
    elif action == "restore":
        if job.deleted_at is not None or job.delist_reason in {"replaced", "expired"}:
            raise BusinessException(40904, "job_not_restorable")
        if job.audit_status != "passed" or job.expires_at is None or job.expires_at <= now:
            raise BusinessException(40904, "job_requires_reaudit")
        if job.delist_reason is None:
            raise BusinessException(40904, "岗位未下架")
        job.delist_reason = None
        job.version = current + 1
        _emit(db, job, "job.restored", reason=reason or "restore")
    elif action == "replace":
        if job.deleted_at is not None:
            raise BusinessException(40904, "job_deleted")
        job.delist_reason = "replaced"
        job.deleted_at = now
        job.version = current + 1
        mark_job_media_delete_pending(db, job.id, include_pending=True)
        _emit(db, job, "job.replaced", reason=reason or "replaced", tombstone=True)
    else:
        raise BusinessException(40101, "unsupported_job_lifecycle_action")
    db.flush()
    return job


def contact_version_is_current(job: Job, expected_version: int | None) -> bool:
    """Contact grants are valid only for a live, unchanged Job version."""
    if expected_version is None or int(job.version or 0) != int(expected_version):
        return False
    now = utcnow()
    return bool(
        job.audit_status == "passed"
        and job.deleted_at is None
        and job.delist_reason is None
        and job.expires_at is not None
        and job.expires_at > now
    )


__all__ = ["contact_version_is_current", "transition_job", "utcnow"]
