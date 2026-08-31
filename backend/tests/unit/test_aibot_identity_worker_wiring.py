from unittest.mock import Mock

from app.services import worker
from app.services.aibot_identity_service import ResolvedActor


class _FakeIdentityClient:
    def __init__(self):
        self.used = False

    def batch_openuserid_to_userid(self, values):
        self.used = True
        raise AssertionError("not called in wiring test")

    def is_canonical_user_visible(self, userid):
        return True, "visible"


def test_worker_wiring_is_fail_closed_when_disabled(monkeypatch):
    monkeypatch.setattr(worker.settings, "identity_resolution_enabled", False)
    service = worker.build_aibot_identity_service()
    assert service.client is None


def test_worker_wiring_injects_identity_client_and_directory_verifier(monkeypatch):
    fake = _FakeIdentityClient()
    monkeypatch.setattr(worker.settings, "identity_resolution_enabled", True)
    monkeypatch.setattr(worker, "_AIBOT_IDENTITY_CLIENT", fake)
    service = worker.build_aibot_identity_service()
    assert service.client is fake
    assert service.verify_plain_userid("canonical-a") == (True, "visible")


def test_worker_maps_transient_directory_failure_to_retry(monkeypatch):
    db = Mock()
    db.query.return_value.filter.return_value.scalar.return_value = None
    monkeypatch.setattr(worker, "SessionLocal", lambda: db)
    monkeypatch.setattr(worker, "build_aibot_identity_service", lambda: Mock(
        resolve_for_event=Mock(return_value=ResolvedActor(
            actor_id="open-a",
            actor_id_kind="open_userid",
            status="conversion_pending",
            reason_code="directory_unavailable",
        )),
    ))

    instance = worker.Worker.__new__(worker.Worker)
    mark_processing = Mock()
    mark_failure = Mock()
    instance._mark_event_processing = mark_processing
    instance._mark_event_fail = mark_failure

    result = instance._process_locked(
        {
            "inbound_event_id": 42,
            "msg_id": "m1",
            "from_userid": "open-a",
            "source_channel": "wecom_aibot",
            "actor_id_kind": "opaque",
            "msg_type": "text",
            "create_time": 1700000000,
        },
        inbound_event_id=42,
        retry_count=1,
        userid="open-a",
    )

    assert result == "identity_pending"
    mark_failure.assert_called_once_with(42, "failed", "identity_directory_unavailable", 2)
    db.close.assert_called_once_with()


def test_worker_marks_revoked_binding_lookup_error_retryable(monkeypatch):
    db = Mock()
    db.query.return_value.filter.return_value.scalar.return_value = None
    monkeypatch.setattr(worker, "SessionLocal", lambda: db)
    monkeypatch.setattr(worker, "build_aibot_identity_service", lambda: Mock(
        resolve_for_event=Mock(side_effect=RuntimeError("database unavailable")),
    ))

    instance = worker.Worker.__new__(worker.Worker)
    instance._mark_event_processing = Mock()
    instance._mark_event_fail = Mock()

    result = instance._process_locked(
        {
            "inbound_event_id": 42,
            "msg_id": "m1",
            "from_userid": "open-a",
            "source_channel": "wecom_aibot",
            "actor_id_kind": "opaque",
            "msg_type": "text",
            "create_time": 1700000000,
        },
        inbound_event_id=42,
        retry_count=1,
        userid="open-a",
    )

    assert result == "identity_resolution_failed"
    instance._mark_event_fail.assert_called_once_with(
        42, "failed", "identity_resolution_failed", 2,
    )
    db.rollback.assert_called_once_with()
    db.close.assert_called_once_with()
