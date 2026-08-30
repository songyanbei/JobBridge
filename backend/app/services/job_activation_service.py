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
    try:
        from app.models import MediaAssetLifecycle
        db.query(MediaAssetLifecycle).filter(
            MediaAssetLifecycle.entity_type == "job",
            MediaAssetLifecycle.entity_id == job.id,
            MediaAssetLifecycle.state == "attached",
        ).update({"entity_version": int(getattr(job, "aggregate_version", None) or job.version)}, synchronize_session=False)
    except Exception:
        # Mixed fleets may still run without the additive media column.
        pass
    job.aggregate_version = int(getattr(job, "aggregate_version", None) or job.version or 1) + 1
    if db is not None and getattr(job, "id", None) is not None:
        db.flush()
        from app.services.domain_outbox_service import append_domain_event
        append_domain_event(
            db,
            aggregate_type="job",
            aggregate_id=int(job.id),
            aggregate_version=int(job.aggregate_version),
            event_type="job.published",
            payload={"job_id": int(job.id), "status": "published", "reason": "activation"},
        )
    return job
