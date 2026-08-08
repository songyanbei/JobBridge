"""The only locking entry points for replacement state transitions."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Job, JobReplacement
from app.services.job_business_digest_service import business_digest

def lock_replacement_creation(db: Session, old_job_id: int, operation_id: str, source_msg_id: str):
    identity = (JobReplacement.operation_id == operation_id) | (JobReplacement.source_msg_id == source_msg_id)
    existing = db.query(JobReplacement).filter(identity).first()
    if existing: return existing, None
    old = db.query(Job).filter(Job.id == old_job_id).with_for_update().first()
    existing = db.query(JobReplacement).filter(identity).first()
    if existing:
        return existing, old
    active = db.query(JobReplacement).filter(
        JobReplacement.active_old_job_id == old_job_id,
    ).with_for_update().first()
    if active:
        return active, old
    return None, old

def lock_replacement_graph(db: Session, replacement_id: int):
    rel = db.query(JobReplacement).filter(JobReplacement.id == replacement_id).first()
    if not rel: return None, [], None
    ids = sorted([rel.old_job_id, rel.new_job_id])
    jobs = db.query(Job).filter(Job.id.in_(ids)).order_by(Job.id).with_for_update().all()
    locked = db.query(JobReplacement).filter(JobReplacement.id == replacement_id).with_for_update().one()
    if (locked.old_job_id, locked.new_job_id, locked.operation_id) != (
        rel.old_job_id, rel.new_job_id, rel.operation_id,
    ):
        raise RuntimeError("replacement graph changed while locking")
    rel = locked
    return rel, jobs, {job.id: job for job in jobs}
