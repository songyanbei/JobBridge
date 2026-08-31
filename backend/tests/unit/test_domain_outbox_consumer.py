from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import DomainOutboxEvent
from app.services.domain_outbox_service import append_domain_event, claim_domain_events, finalize_domain_event


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[DomainOutboxEvent.__table__])
    return sessionmaker(bind=engine)()


def test_claim_fence_and_finalize_success():
    db = _db()
    event = append_domain_event(db, aggregate_type="job", aggregate_id=1, aggregate_version=1, event_type="job.published", payload={})
    db.commit()
    claimed = claim_domain_events(db, owner="worker-a", lease_seconds=60)
    assert claimed and claimed[0].status == "processing" and claimed[0].fencing_token == 1
    assert not finalize_domain_event(db, event.id, owner="worker-b", fencing_token=1)
    assert finalize_domain_event(db, event.id, owner="worker-a", fencing_token=1)
    db.commit()
    assert db.get(DomainOutboxEvent, event.id).status == "published"


def test_failed_event_retries_then_dead_letters():
    db = _db()
    event = append_domain_event(db, aggregate_type="job", aggregate_id=2, aggregate_version=1, event_type="job.published", payload={})
    db.commit()
    claim_domain_events(db, owner="worker-a")
    assert finalize_domain_event(db, event.id, owner="worker-a", fencing_token=1, success=False, max_attempts=2)
    db.commit()
    row = db.get(DomainOutboxEvent, event.id)
    assert row.status == "pending" and row.attempt_count == 1
    row.next_attempt_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    claimed = claim_domain_events(db, owner="worker-a")
    assert claimed
    assert finalize_domain_event(db, event.id, owner="worker-a", fencing_token=2, success=False, max_attempts=2)
    assert db.get(DomainOutboxEvent, event.id).status == "dead_letter"
