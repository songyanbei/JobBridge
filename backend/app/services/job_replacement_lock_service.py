"""The only locking entry points for replacement state transitions."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job, JobReplacement
from app.services.job_business_digest_service import business_digest


@dataclass(frozen=True)
class ReplacementGraphHint:
    id: int
    old_job_id: int
    new_job_id: int
    operation_id: str


def _hint_from_row(row) -> ReplacementGraphHint | None:
    if row is None:
        return None
    return ReplacementGraphHint(
        id=int(row.id),
        old_job_id=int(row.old_job_id),
        new_job_id=int(row.new_job_id),
        operation_id=str(row.operation_id),
    )


def current_active_replacement_hint(
    db: Session, old_job_id: int,
) -> ReplacementGraphHint | None:
    """Read the latest committed active graph without locking relation rows.

    MySQL REPEATABLE READ consistent reads may use a snapshot created before a
    concurrent candidate committed.  A short autocommit connection gives this
    discovery read a fresh view while the caller keeps all formal locks in the
    main transaction.  SQLite tests use the current session because in-memory
    engines commonly share a single connection and have no RR snapshot issue.
    """
    statement = select(
        JobReplacement.id,
        JobReplacement.old_job_id,
        JobReplacement.new_job_id,
        JobReplacement.operation_id,
    ).where(JobReplacement.active_old_job_id == old_job_id)
    bind = db.get_bind()
    if bind.dialect.name == "sqlite":
        row = db.execute(statement).first()
    else:
        engine = getattr(bind, "engine", bind)
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            row = connection.execute(statement).first()
    return _hint_from_row(row)

def lock_replacement_creation(
    db: Session,
    old_job_id: int,
    operation_id: str,
    source_msg_id: str,
):
    identity = (
        (JobReplacement.operation_id == operation_id)
        | (JobReplacement.source_msg_id == source_msg_id)
    )
    existing = db.query(JobReplacement).filter(identity).first()
    if existing:
        return existing, None
    old = (
        db.query(Job)
        .populate_existing()
        .filter(Job.id == old_job_id)
        .with_for_update()
        .first()
    )
    existing = (
        db.query(JobReplacement)
        .populate_existing()
        .filter(identity)
        .with_for_update()
        .first()
    )
    if existing:
        return existing, old
    active = (
        db.query(JobReplacement)
        .populate_existing()
        .filter(JobReplacement.active_old_job_id == old_job_id)
        .with_for_update()
        .first()
    )
    if active:
        return active, old
    return None, old

def lock_replacement_graph(
    db: Session,
    replacement_id: int,
    *,
    hint: ReplacementGraphHint | None = None,
):
    if hint is None:
        rel = db.query(JobReplacement).filter(JobReplacement.id == replacement_id).first()
        hint = _hint_from_row(rel)
    if hint is None:
        return None, [], None
    if int(replacement_id) != hint.id:
        raise RuntimeError("replacement graph hint id mismatch")
    ids = sorted({hint.old_job_id, hint.new_job_id})
    jobs = (
        db.query(Job)
        .populate_existing()
        .filter(Job.id.in_(ids))
        .order_by(Job.id)
        .with_for_update()
        .all()
    )
    locked = (
        db.query(JobReplacement)
        .populate_existing()
        .filter(JobReplacement.id == replacement_id)
        .with_for_update()
        .one()
    )
    if (locked.old_job_id, locked.new_job_id, locked.operation_id) != (
        hint.old_job_id, hint.new_job_id, hint.operation_id,
    ):
        raise RuntimeError("replacement graph changed while locking")
    return locked, jobs, {job.id: job for job in jobs}
