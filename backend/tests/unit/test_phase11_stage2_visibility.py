from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.config import settings
from app.models import Resume
from app.schemas.conversation import SessionState
from app.services import message_router, search_service, upload_service, user_service
from app.services.resume_mutation_service import online_resume_filters


NOW = datetime(2026, 8, 17, 3, 0, 0)


def _sql(expressions) -> str:
    return " ".join(str(item) for item in expressions).lower()


def test_canonical_online_predicate_is_legacy_compatible_then_strict():
    compatible = _sql(online_resume_filters(now=NOW, strict=False))
    strict = _sql(online_resume_filters(now=NOW, strict=True))

    for required in ("audit_status", "deleted_at", "delist_reason", "expires_at"):
        assert required in compatible
    assert "activated_at" not in compatible
    assert "activated_at" in strict


def test_initial_search_and_paging_share_canonical_resume_predicate(monkeypatch):
    monkeypatch.setattr(settings, "resume_lifecycle_v2_enabled", False)
    db = MagicMock()
    query = db.query.return_value.join.return_value
    query.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []

    search_service._query_resumes({"city": ["苏州"]}, 10, db)
    initial = _sql(query.filter.call_args.args)
    assert "delist_reason" in initial
    assert "expires_at" in initial
    assert "activated_at" not in initial

    query.filter.reset_mock()
    query.filter.return_value.all.return_value = []
    search_service._validate_resume_ids(["1"], db)
    paging = _sql(query.filter.call_args.args)
    assert "delist_reason" in paging
    assert "expires_at" in paging


def test_default_criteria_excludes_delisted_and_candidates(monkeypatch):
    monkeypatch.setattr(settings, "resume_lifecycle_v2_enabled", False)
    db = MagicMock()
    query = db.query.return_value
    query.filter.return_value.order_by.return_value.first.return_value = None

    assert message_router._load_worker_resume_defaults("worker-1", db) == {}
    predicate = _sql(query.filter.call_args.args)
    assert "delist_reason" in predicate
    assert "audit_status" in predicate
    assert "expires_at" in predicate


def test_image_append_uses_strict_online_target_predicate(monkeypatch):
    monkeypatch.setattr(settings, "resume_lifecycle_v2_enabled", True)
    db = MagicMock()
    query = db.query.return_value
    query.filter.return_value.with_for_update.return_value.first.return_value = None
    session = SessionState(
        role="worker",
        attachment_target_type="resume",
        attachment_target_id=9,
    )

    upload_service.attach_image("worker-1", "image-key", session, db)
    predicate = _sql(query.filter.call_args.args)
    assert "delist_reason" in predicate
    assert "activated_at" in predicate
    assert "expires_at" in predicate


def test_my_status_separates_online_candidate_and_history(monkeypatch):
    monkeypatch.setattr(settings, "resume_lifecycle_v2_enabled", False)
    user = SimpleNamespace(
        role="worker", status="active", registered_at=NOW - timedelta(days=100),
    )
    online = SimpleNamespace(
        id=3, audit_status="passed", activated_at=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=29), candidate_expires_at=None,
        deleted_at=None, delist_reason=None, created_at=NOW,
    )
    candidate = SimpleNamespace(
        id=2, audit_status="pending", activated_at=None, expires_at=None,
        candidate_expires_at=NOW + timedelta(days=7), deleted_at=None,
        delist_reason=None, created_at=NOW - timedelta(hours=1),
    )
    history = SimpleNamespace(
        id=1, audit_status="passed", activated_at=NOW - timedelta(days=40),
        expires_at=NOW - timedelta(days=10), candidate_expires_at=None,
        deleted_at=None, delist_reason=None, created_at=NOW - timedelta(days=40),
    )

    user_query = MagicMock()
    user_query.filter.return_value.first.return_value = user
    job_query = MagicMock()
    job_query.filter.return_value.order_by.return_value.first.return_value = None
    resume_query = MagicMock()
    resume_query.filter.return_value.order_by.return_value.yield_per.return_value.__iter__.return_value = iter([
        online, candidate, history,
    ])
    db = MagicMock()
    db.query.side_effect = [user_query, job_query, resume_query]

    result = user_service.get_user_status("worker-1", db)
    assert result["latest_online_resume"]["id"] == 3
    assert result["latest_candidate_resume"]["id"] == 2
    assert result["latest_history_resume"]["id"] == 1


def test_my_status_finds_online_resume_beyond_twenty_newer_records(monkeypatch):
    monkeypatch.setattr(settings, "resume_lifecycle_v2_enabled", False)
    user = SimpleNamespace(role="worker", status="active", registered_at=NOW)
    newer = [
        SimpleNamespace(
            id=100 - index,
            audit_status="rejected",
            activated_at=None,
            expires_at=None,
            candidate_expires_at=NOW + timedelta(days=7),
            deleted_at=None,
            delist_reason=None,
            created_at=NOW - timedelta(minutes=index),
        )
        for index in range(21)
    ]
    old_online = SimpleNamespace(
        id=7,
        audit_status="passed",
        activated_at=NOW - timedelta(days=2),
        expires_at=NOW + timedelta(days=28),
        candidate_expires_at=None,
        deleted_at=None,
        delist_reason=None,
        created_at=NOW - timedelta(days=2),
    )

    user_query = MagicMock()
    user_query.filter.return_value.first.return_value = user
    job_query = MagicMock()
    job_query.filter.return_value.order_by.return_value.first.return_value = None
    resume_query = MagicMock()
    stream = resume_query.filter.return_value.order_by.return_value.yield_per.return_value
    stream.__iter__.return_value = iter([*newer, old_online])
    db = MagicMock()
    db.query.side_effect = [user_query, job_query, resume_query]

    result = user_service.get_user_status("worker-1", db)

    assert result["latest_resume"]["id"] == 100
    assert result["latest_candidate_resume"]["id"] == 100
    assert result["latest_online_resume"]["id"] == 7
    resume_query.filter.return_value.order_by.return_value.limit.assert_not_called()
