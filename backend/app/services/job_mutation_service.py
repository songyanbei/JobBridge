"""Shared guards and version rules for Job business mutations."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.exceptions import BusinessException
from app.models import Job, JobReplacement, MediaAssetLifecycle


def assert_job_activated(job: Job) -> None:
    if job.activated_at is None or job.expires_at is None:
        raise BusinessException(40904, "job_not_activated")


def active_replacement(db: Session, job_id: int) -> JobReplacement | None:
    row = db.query(JobReplacement).filter(JobReplacement.active_old_job_id == job_id).first()
    return row if isinstance(row, JobReplacement) else None


def reject_if_replacement_in_progress(db: Session, job_id: int) -> None:
    if active_replacement(db, job_id):
        raise BusinessException(40904, "replacement_in_progress")


def increment_version(job: Job) -> None:
    job.version = int(job.version or 0) + 1


def close_active_replacement(db: Session, old_job: Job, *, reason: str) -> None:
    relation = db.query(JobReplacement).filter(
        JobReplacement.active_old_job_id == old_job.id,
    ).with_for_update().first()
    if not isinstance(relation, JobReplacement):
        return
    candidate = db.query(Job).filter(Job.id == relation.new_job_id).with_for_update().first()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    relation.lifecycle_status = "closed"
    relation.closed_reason = reason
    relation.active_old_job_id = None
    if candidate and candidate.expires_at is None and candidate.deleted_at is None:
        candidate.deleted_at = now
        candidate.candidate_expires_at = None
        increment_version(candidate)
        db.query(MediaAssetLifecycle).filter(
            MediaAssetLifecycle.entity_type == "job",
            MediaAssetLifecycle.entity_id == candidate.id,
            MediaAssetLifecycle.state.in_(("pending", "attached")),
        ).update({
            "state": "delete_pending",
            "next_attempt_at": now,
        }, synchronize_session=False)
