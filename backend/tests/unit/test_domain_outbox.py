from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError
from unittest.mock import MagicMock

from app.db import Base
from app.models import DomainOutboxEvent
from app.services.domain_outbox_service import append_domain_event, event_is_current


def test_domain_event_is_versioned_unique_and_pii_safe():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[DomainOutboxEvent.__table__])
    db = sessionmaker(bind=engine)()
    event = append_domain_event(
        db, aggregate_type="job", aggregate_id=7, aggregate_version=2,
        event_type="job.published", payload={"job_id": 7, "phone": "13800138000", "status": "published"},
        trace_id="trace-1",
    )
    db.commit()
    assert event.payload == {"job_id": 7, "status": "published"}
    assert len(event.payload_digest) == 64
    assert event_is_current(event, current_version=2, active=True)
    assert not event_is_current(event, current_version=1, active=True)
    assert not event_is_current(event, current_version=2, active=False)


def test_domain_event_duplicate_is_idempotent():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[DomainOutboxEvent.__table__])
    db = sessionmaker(bind=engine)()
    kwargs = dict(aggregate_type="job", aggregate_id=1, aggregate_version=1, event_type="job.published", payload={"job_id": 1})
    first = append_domain_event(db, **kwargs)
    db.commit()
    second = append_domain_event(db, **kwargs)
    assert second.id == first.id


def test_integrity_race_uses_savepoint_without_outer_rollback():
    existing = MagicMock(id=9)
    db = MagicMock()
    nested = MagicMock()
    nested.__enter__.return_value = nested
    db.begin_nested.return_value = nested
    db.flush.side_effect = IntegrityError("insert", {}, Exception("duplicate"))
    db.query.return_value.filter_by.return_value.first.side_effect = [None, existing]
    result = append_domain_event(
        db, aggregate_type="job", aggregate_id=9, aggregate_version=2,
        event_type="job.updated", payload={"job_id": 9},
    )
    assert result is existing
    db.rollback.assert_not_called()
    db.begin_nested.assert_called_once()
