from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import DomainOutboxEvent, Resume, ResumeReplacement
from app.services import audit_workbench_service, resume_admin_service
from app.services.resume_mutation_service import append_resume_domain_event


def _online_resume(**overrides):
    values = dict(
        id=7, version=1, aggregate_version=1, audit_status="passed",
        activated_at=datetime.utcnow(), expires_at=datetime.utcnow() + timedelta(days=5),
        deleted_at=None, delist_reason=None, description="old",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_resume_admin_update_cas_writes_aggregate_version_and_event(monkeypatch):
    db = MagicMock()
    row = _online_resume()
    update_query = MagicMock()
    update_query.update.return_value = 1
    db.query.return_value.filter.return_value = update_query
    db.query.return_value.populate_existing.return_value.filter.return_value.first.return_value = row
    monkeypatch.setattr(resume_admin_service, "lock_resume", lambda *_: row)
    monkeypatch.setattr(resume_admin_service, "reject_if_replacement_in_progress", lambda *_: None)
    monkeypatch.setattr(resume_admin_service, "write_admin_log", lambda *a, **k: None)
    events = []
    monkeypatch.setattr(
        resume_admin_service, "append_resume_domain_event",
        lambda *a, **k: events.append((a, k)),
    )

    resume_admin_service.update_resume(db, 7, 1, {"description": "new"}, "admin-1")

    patch = update_query.update.call_args.args[0]
    assert patch["version"] == 2
    assert patch["aggregate_version"] == 2
    assert events and events[0][0][1] is row
    db.commit.assert_called_once()


def test_audit_resume_reject_and_edit_emit_events_with_aggregate_increment(monkeypatch):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    row = _online_resume(audit_status="pending", activated_at=None, expires_at=None,
                         candidate_expires_at=datetime.utcnow() + timedelta(days=2))
    monkeypatch.setattr(audit_workbench_service, "write_admin_log", lambda *a, **k: None)
    monkeypatch.setattr(audit_workbench_service, "get_resume_candidate_ttl_days", lambda *_: 7, raising=False)
    monkeypatch.setattr("app.services.resume_mutation_service.lock_resume", lambda *_: row)
    monkeypatch.setattr("app.services.resume_mutation_service.reject_if_replacement_in_progress", lambda *_: None)
    events = []
    monkeypatch.setattr(
        "app.services.resume_mutation_service.append_resume_domain_event",
        lambda *a, **k: events.append((a, k)),
    )

    audit_workbench_service._reject_resume(db, 7, 1, "sensitive free text", "admin-1", block_user=False)
    assert row.version == 2 and row.aggregate_version == 2
    assert events and events[-1][1]["payload"] == {"status": "rejected", "reason_code": "manual_reject"}

    row.audit_status = "pending"
    row.candidate_expires_at = datetime.utcnow() + timedelta(days=2)
    audit_workbench_service.edit_action(db, "resume", 7, 2, {"description": "edited"}, "admin-1")
    assert row.version == 3 and row.aggregate_version == 3
    assert events[-1][1]["payload"]["reason"] == "audit_edit"


def test_resume_domain_event_uses_aggregate_version_and_strips_pii():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[DomainOutboxEvent.__table__])
    db = Session(engine)
    resume = SimpleNamespace(id=9, version=4, aggregate_version=5)

    append_resume_domain_event(
        db, resume, "resume.updated",
        payload={"status": "updated", "phone": "13800000000"},
    )
    db.commit()

    event = db.query(DomainOutboxEvent).one()
    assert event.aggregate_version == 5
    assert event.payload == {"status": "updated", "resume_id": 9}
