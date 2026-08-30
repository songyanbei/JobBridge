from datetime import datetime
from types import SimpleNamespace
from app.services.job_activation_service import activate_job
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as OrmSession

def test_activation_starts_ttl_at_activation_time(monkeypatch):
    monkeypatch.setattr('app.services.job_activation_service.get_job_ttl_days', lambda _: 30)
    job = SimpleNamespace(audit_status='pending', expires_at=None, candidate_expires_at=1, version=1)
    now = datetime(2026, 1, 1)
    activate_job(None, job, now)
    assert job.expires_at == datetime(2026, 1, 31) and job.candidate_expires_at is None


def test_activation_skips_outbox_when_phase14_table_is_absent(monkeypatch):
    monkeypatch.setattr('app.services.job_activation_service.get_job_ttl_days', lambda _: 30)
    engine = create_engine('sqlite:///:memory:')
    with OrmSession(engine) as db:
        job = SimpleNamespace(
            id=7, audit_status='pending', expires_at=None,
            candidate_expires_at=1, version=1,
        )
        activate_job(db, job, datetime(2026, 1, 1))
        assert job.version == 2
