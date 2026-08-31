from datetime import datetime, timedelta
from types import SimpleNamespace

from app.services.domain_outbox_service import job_event_is_current


def _event(kind, version=3, tombstone=False):
    return SimpleNamespace(aggregate_type=kind, aggregate_id=7, aggregate_version=version, tombstone=tombstone)


class _Query:
    def __init__(self, row): self.row = row
    def filter(self, *args): return self
    def populate_existing(self): return self
    def first(self): return self.row


class _Db:
    def __init__(self, row): self.row = row
    def query(self, model): return _Query(self.row)


def test_resume_event_requires_exact_version_and_online_state():
    row = SimpleNamespace(aggregate_version=3, version=3, audit_status="passed", deleted_at=None, delist_reason=None, expires_at=datetime.now() + timedelta(days=1))
    db = _Db(row)
    assert job_event_is_current(db, _event("resume", 3))
    assert not job_event_is_current(db, _event("resume", 2))
    row.deleted_at = datetime.now()
    assert not job_event_is_current(db, _event("resume", 3))
    assert job_event_is_current(db, _event("resume", 3, tombstone=True))


def test_unknown_aggregate_type_fails_closed():
    assert not job_event_is_current(_Db(SimpleNamespace()), _event("listing", 1))
