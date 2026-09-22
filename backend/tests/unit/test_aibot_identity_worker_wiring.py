import json
from unittest.mock import Mock

from sqlalchemy.dialects import mysql

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
    instance._handle_error = Mock()

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
    mark_failure.assert_not_called()
    assert instance._handle_error.call_args.args[1:3] == (42, 1)
    assert str(instance._handle_error.call_args.args[3]) == "identity_directory_unavailable"
    assert instance._handle_error.call_args.kwargs == {"send_fallback": False}
    assert db.commit.call_count >= 2
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
    instance._handle_error = Mock()

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
    instance._mark_event_fail.assert_not_called()
    assert instance._handle_error.call_args.args[1:3] == (42, 1)
    assert str(instance._handle_error.call_args.args[3]) == "identity_resolution_failed"
    assert instance._handle_error.call_args.kwargs == {"send_fallback": False}
    db.rollback.assert_called_once_with()
    db.close.assert_called_once_with()


def test_aibot_identity_retry_is_requeued_then_terminalized_without_legacy_send(monkeypatch):
    instance = worker.Worker.__new__(worker.Worker)
    instance._wecom_client = Mock()
    instance._mark_event_fail = Mock()
    instance._mark_aibot_identity_dead_letter = Mock()
    message = {"source_channel": "wecom_aibot", "from_userid": "opaque-actor"}
    queued = []
    monkeypatch.setattr(worker, "enqueue_message", lambda payload, queue: queued.append((json.loads(payload), queue)))

    instance._handle_error(message.copy(), 42, 1, RuntimeError("identity_resolution_failed"), send_fallback=False)
    assert queued[0][0]["_retry_count"] == 2
    assert queued[0][1] == worker.QUEUE_INCOMING
    instance._mark_event_fail.assert_called_once_with(42, "failed", "RuntimeError: identity_resolution_failed", 2)

    instance._handle_error(message.copy(), 42, worker.MAX_RETRY, RuntimeError("identity_resolution_failed"), send_fallback=False)
    assert queued[1][1] == worker.QUEUE_DEAD_LETTER
    instance._mark_aibot_identity_dead_letter.assert_called_once_with(
        42, "RuntimeError: identity_resolution_failed", worker.MAX_RETRY + 1,
    )
    instance._wecom_client.send_text.assert_not_called()


def test_order_gate_ignores_exhausted_failed_event(monkeypatch):
    db = Mock()
    query = db.query.return_value.filter.return_value
    query.filter.return_value.first.return_value = None
    monkeypatch.setattr(worker, "SessionLocal", lambda: db)
    instance = worker.Worker.__new__(worker.Worker)

    assert not instance._has_earlier_unfinished_event("actor", 50)

    predicate = db.query.return_value.filter.call_args.args[1]
    sql = str(predicate.compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "wecom_inbound_event.retry_count <= 2" in sql
    assert "wecom_inbound_event.status = 'failed'" in sql


def test_startup_recovery_only_requeues_retryable_failed_events(monkeypatch):
    db = Mock()
    db.query.return_value.filter.return_value.with_for_update.return_value.limit.return_value.all.return_value = []
    monkeypatch.setattr(worker, "SessionLocal", lambda: db)
    instance = worker.Worker.__new__(worker.Worker)

    instance._startup_recovery()

    predicate = db.query.return_value.filter.call_args.args[0]
    sql = str(predicate.compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "wecom_inbound_event.retry_count <= 2" in sql
    assert "wecom_inbound_event.status = 'failed'" in sql


def test_identity_terminal_reply_uses_aibot_outbox_not_legacy_client(monkeypatch):
    from types import SimpleNamespace
    from app.models import WecomOutboundOutbox

    db = Mock()
    event = SimpleNamespace(
        status="processing", provider_req_id="req-1", from_userid="opaque-actor",
        source_channel="wecom_aibot",
        conversation_type="single", conversation_id="opaque-actor",
        chat_id=None, ordering_key="single:opaque-actor", created_at=None,
    )
    db.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = event
    db.query.return_value.filter.return_value.first.return_value = None
    db.get.return_value = event
    monkeypatch.setattr(worker, "SessionLocal", lambda: db)
    instance = worker.Worker.__new__(worker.Worker)
    instance._wecom_client = Mock()

    instance._mark_aibot_identity_dead_letter(42, "identity_error", 3)

    assert event.status == "dead_letter"
    assert event.retry_count == 3
    outbox = db.add.call_args.args[0]
    assert isinstance(outbox, WecomOutboundOutbox)
    assert outbox.channel == "wecom_aibot"
    assert outbox.reply_command == "aibot_respond_msg"
    assert outbox.provider_req_id == "req-1"
    assert outbox.content == worker.DEAD_LETTER_REPLY
    assert outbox.userid == "opaque-actor"
    db.commit.assert_called_once()
    instance._wecom_client.send_text.assert_not_called()
