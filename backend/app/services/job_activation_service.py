"""Short transaction activation primitive shared by create and replace flows."""
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from app.models import Job
from app.services.lifecycle_config_service import get_job_ttl_days

def activate_job(db: Session, job: Job, now: datetime | None = None) -> Job:
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    if job.audit_status == "passed" and job.expires_at is not None:
        return job
    job.audit_status = "passed"
    job.activated_at = now
    job.expires_at = now + timedelta(days=get_job_ttl_days(db))
    job.candidate_expires_at = None
    job.version = int(job.version or 0) + 1
    if db is not None:
        db.flush()
    return job
