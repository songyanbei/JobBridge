"""Short transaction activation primitive shared by create and replace flows."""
from datetime import datetime, timedelta, timezone
from sqlalchemy import inspect
from sqlalchemy.orm import Session
from app.models import Job
from app.services.lifecycle_config_service import get_job_ttl_days


def _domain_outbox_available(db: Session | None) -> bool:
    """Return whether the additive Phase14 event table exists on this bind."""
    bind = getattr(db, "bind", None) if db is not None else None
    if bind is None:
        return False
    try:
        # SQLite in-memory engines commonly share one connection between the
        # Session and Inspector.  Opening an Inspector on the Engine while a
        # transaction is active can roll that transaction back when the
        # inspector closes.  Inspect the session connection in that case.
        if (
            getattr(getattr(bind, "dialect", None), "name", None) == "sqlite"
            and db.in_transaction()
        ):
            return bool(inspect(db.connection()).has_table("domain_outbox_event"))
        return bool(inspect(bind).has_table("domain_outbox_event"))
    except Exception:
        # A temporarily unavailable inspector must not turn a legacy activation
        # into a rollback; the caller can reconcile the event later.
        return False

def activate_job(db: Session, job: Job, now: datetime | None = None) -> Job:
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    if job.audit_status == "passed" and job.expires_at is not None:
        return job
    job.audit_status = "passed"
    job.activated_at = now
    job.expires_at = now + timedelta(days=get_job_ttl_days(db))
    job.candidate_expires_at = None
    current_version = int(job.version or 0)
    current_aggregate = int(getattr(job, "aggregate_version", None) or 0)
    job.version = current_version + 1
    job.aggregate_version = max(current_version, current_aggregate) + 1
    try:
        from app.models import MediaAssetLifecycle
        db.query(MediaAssetLifecycle).filter(
            MediaAssetLifecycle.entity_type == "job",
            MediaAssetLifecycle.entity_id == job.id,
            MediaAssetLifecycle.state == "attached",
        ).update({"entity_version": int(job.aggregate_version)}, synchronize_session=False)
    except Exception:
        # Mixed fleets may still run without the additive media column.
        pass
    if db is not None and getattr(job, "id", None) is not None:
        db.flush()
        if _domain_outbox_available(db):
            try:
                from app.services.domain_outbox_service import append_domain_event
            except ImportError:
                append_domain_event = None
            if append_domain_event is not None and getattr(job, "id", None) is not None:
                append_domain_event(
                    db,
                    aggregate_type="job",
                    aggregate_id=int(job.id),
                    aggregate_version=int(getattr(job, "aggregate_version", None) or job.version),
                    event_type="job.published",
                    payload={"job_id": int(job.id), "status": "published", "reason": "activation"},
                )
    return job
