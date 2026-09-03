import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services.inbound_acceptance import InboundAcceptanceService
from app.wecom.callback import WeComMessage


class FakeDB:
    def __init__(self, existing=None):
        self.existing = existing
        self.added = None

    def query(self, *_args):
        query = MagicMock()
        query.filter.return_value.first.return_value = self.existing
        return query

    def add(self, event):
        self.added = event
        event.id = 42
        event.created_at = datetime(2026, 8, 31, 1, 2, 3)
        event.retry_count = 0

    def commit(self):
        return None

    def refresh(self, _event):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


def _aibot_message(**kwargs):
    values = dict(
        msg_id="ignored-internal-id",
        provider_msg_id="provider-msg-001",
        from_user="opaque-user",
        msg_type="text",
        content="hello",
        source_channel="wecom_aibot",
        conversation_type="single",
        conversation_id="opaque-user",
        provider_req_id="req-1",
        aibot_id="bot-1",
    )
    values.update(kwargs)
    return WeComMessage(**values)


def test_accept_aibot_builds_canonical_id_and_schema_v2_payload():
    db = FakeDB()
    queued = []
    result = InboundAcceptanceService(
        db_factory=lambda: db,
        duplicate_check=lambda _key: False,
        enqueue=lambda payload, queue: queued.append((payload, queue)),
    ).accept(_aibot_message())

    assert result.status == "accepted"
    assert result.event_id == 42
    assert db.added.msg_id.startswith("aibot_")
    assert len(db.added.msg_id) == 64
    envelope = json.loads(queued[0][0])
    assert envelope["schema_version"] == 2
    assert envelope["provider_msg_id"] == "provider-msg-001"
    assert envelope["source_channel"] == "wecom_aibot"
    assert envelope["ordering_key"] == "wecom:wecom_aibot:single:opaque-user"
    assert "media_url" not in envelope
    assert "media_aes_key" not in envelope


def test_l1_hit_still_checks_db_and_returns_duplicate_without_enqueue():
    existing = MagicMock(id=9)
    db = FakeDB(existing=existing)
    enqueue = MagicMock()
    result = InboundAcceptanceService(
        db_factory=lambda: db,
        duplicate_check=lambda _key: True,
        enqueue=enqueue,
    ).accept(_aibot_message())

    assert result.status == "duplicate"
    assert result.event_id == 9
    enqueue.assert_not_called()


def test_stale_l1_marker_does_not_drop_message():
    db = FakeDB()
    result = InboundAcceptanceService(
        db_factory=lambda: db,
        duplicate_check=lambda _key: True,
        enqueue=lambda *_args: None,
    ).accept(_aibot_message())
    assert result.status == "accepted"
    assert db.added is not None


def test_aibot_enqueue_failure_is_retryable_and_not_acknowledged():
    db = FakeDB()
    result = InboundAcceptanceService(
        db_factory=lambda: db,
        duplicate_check=lambda _key: False,
        enqueue=MagicMock(side_effect=ConnectionError("redis down")),
    ).accept(_aibot_message())
    assert result.status == "retryable"
    assert not result.acknowledged
    assert result.event_id == 42


def test_aibot_db_failure_is_retryable():
    result = InboundAcceptanceService(
        db_factory=MagicMock(side_effect=ConnectionError("mysql down")),
        duplicate_check=lambda _key: False,
        enqueue=MagicMock(),
    ).accept(_aibot_message())
    assert result.status == "retryable"


def test_aibot_media_is_encrypted_during_durable_acceptance():
    db = FakeDB()
    msg = _aibot_message(
        msg_type="image",
        media_id="media-1",
        media_url="https://example.invalid/media",
        media_aes_key="aes-key",
        media_expires_at=1_900_000_000,
    )

    class FakeCrypto:
        @classmethod
        def from_settings(cls):
            return cls()

        def encrypt(self, value, **_kwargs):
            return SimpleNamespace(value=("sealed:" + value).encode())

    queued = []
    with patch("app.services.pii_crypto_service.PiiCryptoService", FakeCrypto):
        result = InboundAcceptanceService(
            db_factory=lambda: db,
            duplicate_check=lambda _key: False,
            enqueue=lambda payload, queue: queued.append(payload),
        ).accept(msg)

    assert result.status == "accepted"
    assert db.added.media_url_ciphertext == b"sealed:https://example.invalid/media"
    assert db.added.media_aes_key_ciphertext == b"sealed:aes-key"
    assert db.added.media_expires_at == datetime.fromtimestamp(1_900_000_000, timezone.utc).replace(tzinfo=None)
    envelope = json.loads(queued[0])
    assert envelope["media_expires_at"] == 1_900_000_000
    assert "https://example.invalid/media" not in queued[0]
    assert "aes-key" not in queued[0]


def test_aibot_media_missing_url_is_invalid():
    db = FakeDB()
    result = InboundAcceptanceService(
        db_factory=lambda: db,
        duplicate_check=lambda _key: False,
        enqueue=MagicMock(),
    ).accept(_aibot_message(msg_type="image", media_id="media-1"))

    assert result.status == "invalid"
    assert db.added is None


def test_aibot_media_without_provider_expiry_uses_five_minute_url_window():
    db = FakeDB()
    msg = _aibot_message(
        msg_type="image",
        media_id="media-1",
        media_url="https://example.invalid/media",
        media_aes_key="aes-key",
        media_expires_at=None,
    )
    with patch("app.services.inbound_acceptance.time.time", return_value=1_900_000_000):
        result = InboundAcceptanceService(
            db_factory=lambda: db,
            duplicate_check=lambda _key: False,
            enqueue=lambda *_args: None,
        ).accept(msg)

    assert result.status == "accepted"
    assert db.added.media_expires_at == datetime.fromtimestamp(1_900_000_300, timezone.utc).replace(tzinfo=None)


def test_aibot_voice_transcript_does_not_require_download_media_fields():
    db = FakeDB()
    result = InboundAcceptanceService(
        db_factory=lambda: db,
        duplicate_check=lambda _key: False,
        enqueue=lambda *_args: None,
    ).accept(_aibot_message(msg_type="voice", content="语音转写"))

    assert result.status == "accepted"


def test_aibot_rate_limit_uses_conversation_and_actor_keys_and_never_enqueues():
    db = FakeDB()
    queued = []
    checks = []

    def rate_check(key, window, max_count):
        checks.append((key, window, max_count))
        return not key.endswith("opaque-user")

    result = InboundAcceptanceService(
        db_factory=lambda: db,
        duplicate_check=lambda _key: False,
        enqueue=lambda payload, queue: queued.append((payload, queue)),
        aibot_rate_check=rate_check,
    ).accept(_aibot_message())

    assert result.status == "retryable"
    assert not result.acknowledged
    assert queued == []
    assert db.added.rate_limit_decision == "rate_limited"
    assert db.added.rate_limit_rule == "aibot.conversation_actor.v1"
    assert {key for key, _window, _limit in checks} == {
        "rate:wecom_aibot:single:opaque-user",
        "rate:wecom_aibot:actor:opaque-user",
    }
    assert {(window, limit) for _key, window, limit in checks} == {(60, 30), (3600, 1000)}


def test_aibot_rate_limited_duplicate_does_not_ack_or_enqueue():
    existing = MagicMock(id=17, rate_limit_decision="rate_limited")
    db = FakeDB(existing=existing)
    enqueue = MagicMock()
    result = InboundAcceptanceService(
        db_factory=lambda: db,
        duplicate_check=lambda _key: True,
        enqueue=enqueue,
        aibot_rate_check=MagicMock(),
    ).accept(_aibot_message())
    assert result.status == "retryable"
    assert not result.acknowledged
    enqueue.assert_not_called()
