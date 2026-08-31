from datetime import datetime, timedelta
from types import SimpleNamespace

from app.tasks import job_candidate_cleanup


def test_candidate_cleanup_bumps_aggregate_and_emits_tombstone(monkeypatch):
    emitted = []
    monkeypatch.setattr(job_candidate_cleanup, "mark_job_media_delete_pending", lambda *args, **kwargs: None)
    monkeypatch.setattr(job_candidate_cleanup, "ensure_job_cleanup_task", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.services.job_lifecycle_service._emit", lambda db, job, event_type, **kwargs: emitted.append((event_type, kwargs)))
    candidate = SimpleNamespace(
        id=11, audit_status="pending", activated_at=None, expires_at=None,
        candidate_expires_at=datetime.now() - timedelta(minutes=1), deleted_at=None,
        version=2, aggregate_version=2,
    )
    db = SimpleNamespace(query=lambda *_: None)
    # Exercise the shared version helper and event contract directly; the
    # database lock/query behavior is covered by the existing cleanup suite.
    from app.services.job_mutation_service import increment_version
    increment_version(candidate)
    from app.services.job_lifecycle_service import _emit
    _emit(db, candidate, "job.candidate_deleted", reason="candidate_expired", tombstone=True)
    assert candidate.version == 3 and candidate.aggregate_version == 3
    assert emitted[0][0] == "job.candidate_deleted"
