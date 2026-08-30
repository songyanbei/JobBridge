from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.job_activation_service import activate_job


def test_activation_increments_aggregate_and_emits_published_event(monkeypatch):
    emitted = []
    monkeypatch.setattr("app.services.job_activation_service.get_job_ttl_days", lambda _: 30)
    monkeypatch.setattr(
        "app.services.domain_outbox_service.append_domain_event",
        lambda db, **kwargs: emitted.append(kwargs),
    )
    db = MagicMock()
    job = SimpleNamespace(
        id=42, audit_status="pending", expires_at=None, candidate_expires_at=1,
        version=3, aggregate_version=3,
    )
    activate_job(db, job, datetime(2026, 1, 1))
    assert job.version == 4
    assert job.aggregate_version == 4
    assert emitted[0]["event_type"] == "job.published"
    assert emitted[0]["aggregate_version"] == 4
