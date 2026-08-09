"""Shared guards and version rules for Job business mutations."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.exceptions import BusinessException
from app.models import Job, JobReplacement, MediaAssetLifecycle
from app.services.job_replacement_lock_service import (
    current_active_replacement_hint,
    lock_replacement_graph,
)


def assert_job_activated(job: Job) -> None:
    if job.activated_at is None or job.expires_at is None:
        raise BusinessException(40904, "job_not_activated")


def active_replacement(db: Session, job_id: int) -> JobReplacement | None:
    row = db.query(JobReplacement).filter(JobReplacement.active_old_job_id == job_id).first()
    return row if isinstance(row, JobReplacement) else None


def lock_job_for_mutation(db: Session, job_id: int) -> Job | None:
    """Take a current row lock and refresh any stale identity-map instance."""
    return (
        db.query(Job)
        .populate_existing()
        .filter(Job.id == job_id)
        .with_for_update()
        .first()
    )


def lock_active_job_for_owner(
    db: Session,
    job_id: int,
    owner_userid: str,
    now: datetime,
) -> Job | None:
    """Lock and recheck the online Job immediately before a user mutation."""
    return (
        db.query(Job)
        .populate_existing()
        .filter(
            Job.id == job_id,
            Job.owner_userid == owner_userid,
            Job.audit_status == "passed",
            Job.activated_at.isnot(None),
            Job.deleted_at.is_(None),
            Job.delist_reason.is_(None),
            Job.expires_at.isnot(None),
            Job.expires_at > now,
        )
        .with_for_update()
        .first()
    )


def reject_if_replacement_in_progress(
    db: Session,
    job_id: int,
    *,
    lock_relation: bool = False,
) -> None:
    query = db.query(JobReplacement).filter(
        JobReplacement.active_old_job_id == job_id,
    )
    if lock_relation:
        query = query.with_for_update()
    row = query.first()
    if isinstance(row, JobReplacement):
        raise BusinessException(40904, "replacement_in_progress")


def increment_version(job: Job) -> None:
    """Increment an already locked or transaction-local Job instance."""
    job.version = int(job.version or 0) + 1


def _active_for_owner(job: Job, owner_userid: str, active_at: datetime) -> bool:
    expires_at = job.expires_at
    compare_at = active_at
    if expires_at is not None and expires_at.tzinfo is not None and compare_at.tzinfo is None:
        compare_at = compare_at.replace(tzinfo=timezone.utc)
    elif expires_at is not None and expires_at.tzinfo is None and compare_at.tzinfo is not None:
        compare_at = compare_at.replace(tzinfo=None)
    return bool(
        job.owner_userid == owner_userid
        and job.audit_status == "passed"
        and job.activated_at is not None
        and job.deleted_at is None
        and job.delist_reason is None
        and expires_at is not None
        and expires_at > compare_at
    )


def close_active_replacement(
    db: Session,
    old_job: Job,
    *,
    reason: str,
    owner_userid: str | None = None,
    active_at: datetime | None = None,
) -> Job | None:
    """Lock an old Job and atomically close its candidate, if one exists."""
    hint = current_active_replacement_hint(db, old_job.id)
    relation = None
    jobs = None
    if hint is not None:
        relation, _, jobs = lock_replacement_graph(db, hint.id, hint=hint)
        locked_old = jobs.get(old_job.id) if isinstance(jobs, dict) else None
    else:
        locked_old = lock_job_for_mutation(db, old_job.id)
        if locked_old is not None:
            # Candidate creation also serializes on old Job.  A relation first
            # committed while we waited is therefore stable for this recheck.
            hint = current_active_replacement_hint(db, old_job.id)
            if hint is not None:
                if hint.new_job_id <= hint.old_job_id:
                    raise RuntimeError("replacement_creation_job_id_invariant_violated")
                relation, _, jobs = lock_replacement_graph(db, hint.id, hint=hint)
                locked_old = jobs.get(old_job.id) if isinstance(jobs, dict) else None
    if locked_old is None:
        raise BusinessException(40401, "岗位不存在")
    if owner_userid is not None:
        if active_at is None:
            raise ValueError("active_at_required_for_owner_recheck")
        if not _active_for_owner(locked_old, owner_userid, active_at):
            return None
    if not isinstance(relation, JobReplacement):
        return locked_old
    if (
        not isinstance(jobs, dict)
        or relation.active_old_job_id != locked_old.id
    ):
        return locked_old
    candidate = jobs.get(relation.new_job_id)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    relation.lifecycle_status = "closed"
    relation.closed_reason = reason
    relation.active_old_job_id = None
    relation.candidate_cleaned_at = now
    if candidate and candidate.expires_at is None and candidate.deleted_at is None:
        candidate.deleted_at = now
        increment_version(candidate)
        db.query(MediaAssetLifecycle).filter(
            MediaAssetLifecycle.entity_type == "job",
            MediaAssetLifecycle.entity_id == candidate.id,
            MediaAssetLifecycle.state.in_(("pending", "attached")),
        ).update({
            "state": "delete_pending",
            "next_attempt_at": now,
        }, synchronize_session=False)
        from app.services.target_cleanup_service import ensure_job_cleanup_task
        ensure_job_cleanup_task(db, candidate.id, reason="candidate_cancelled")
    return locked_old
