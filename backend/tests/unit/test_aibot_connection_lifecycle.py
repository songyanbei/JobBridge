import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.aibot_connection import (
    AibotConnection,
    AibotOutboxWriter,
    stable_aibot_ack_req_id,
)
from app.wecom.aibot_client import AibotClient, AibotClientError
from app.wecom.aibot_transport import AibotTransport, TransportState


class AckSocket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.closed = False

    async def send(self, payload):
        frame = json.loads(payload)
        self.sent.append(frame)
        await self.incoming.put(json.dumps({
            "headers": {"req_id": frame["headers"]["req_id"]},
            "errcode": 0,
            "errmsg": "ok",
        }))

    async def recv(self):
        return await self.incoming.get()

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_real_transport_adapts_outbox_to_protocol_frame_and_ack():
    socket = AckSocket()
    transport = AibotTransport(
        AibotClient("BOTID", "SECRET"),
        connect_factory=lambda _url: socket,
        lease_acquire=lambda: (True, 7),
        lease_renew=lambda _token: True,
        heartbeat_seconds=60,
    )

    assert await transport.connect_once()
    result = await transport.send_outbox({
        "id": 9,
        "reply_index": 0,
        "content": "hello",
        "provider_req_id": "req-1",
        "reply_command": "aibot_respond_msg",
    })

    assert result == {"headers": {"req_id": "req-1"}, "errcode": 0, "errmsg": "ok"}
    assert socket.sent[-1]["cmd"] == "aibot_respond_msg"
    assert socket.sent[-1]["headers"]["req_id"] == "req-1"
    assert socket.sent[-1]["body"]["msgtype"] == "stream"
    assert socket.sent[-1]["body"]["stream"]["finish"] is True
    assert socket.sent[-1]["body"]["stream"]["id"]
    await transport.close()
    assert transport.state == TransportState.ACTIVE


@pytest.mark.asyncio
async def test_active_push_uses_ack_req_id_and_rejects_missing_value():
    socket = AckSocket()
    transport = AibotTransport(
        AibotClient("BOTID", "SECRET"),
        connect_factory=lambda _url: socket,
        lease_acquire=lambda: (True, 7),
        lease_renew=lambda _token: True,
        heartbeat_seconds=60,
    )

    assert await transport.connect_once()
    result = await transport.send_outbox({
        "content": "hello",
        "provider_req_id": "source-req",
        "ack_req_id": "aibot-send-42-stable",
        "reply_command": "aibot_send_msg",
        "chat_id": "chat-1",
    })
    assert result["headers"]["req_id"] == "aibot-send-42-stable"
    assert socket.sent[-1]["cmd"] == "aibot_send_msg"
    assert socket.sent[-1]["headers"]["req_id"] == "aibot-send-42-stable"
    with pytest.raises(AibotClientError, match="ack_req_id"):
        await transport.send_outbox({"content": "missing", "reply_command": "aibot_send_msg"})
    await transport.close()


@pytest.mark.asyncio
async def test_async_writer_awaits_transport_and_accepts_mapping_ack(monkeypatch):
    class AsyncTransport:
        async def send_outbox(self, item):
            return {"headers": {"req_id": item["provider_req_id"]}, "errcode": 0, "errmsg": "ok"}

    writer = AibotOutboxWriter(transport=AsyncTransport(), lease_owner="owner", fencing_token=1)
    item = {"id": 1, "provider_req_id": "req-1"}
    marked = []
    monkeypatch.setattr(writer, "_mark_sent", lambda current, response: marked.append((current, response)) or True)
    assert await writer.deliver_async(item)
    assert marked and marked[0][1]["errcode"] == 0


def test_active_push_ack_req_id_is_stable_and_required():
    ack_req_id = stable_aibot_ack_req_id(42, "provider-1")
    assert ack_req_id == stable_aibot_ack_req_id(42, "provider-1")
    assert AibotOutboxWriter._valid_ack(
        {"headers": {"req_id": ack_req_id}, "errcode": 0},
        {"reply_command": "aibot_send_msg", "ack_req_id": ack_req_id},
    )
    assert not AibotOutboxWriter._valid_ack(
        {"headers": {"req_id": "wrong"}, "errcode": 0},
        {"reply_command": "aibot_send_msg", "ack_req_id": ack_req_id},
    )
    assert not AibotOutboxWriter._valid_ack(
        {"headers": {"req_id": ack_req_id}, "errcode": 0},
        {"reply_command": "aibot_send_msg"},
    )


def test_release_lease_handles_bytes_from_redis(monkeypatch):
    class Redis:
        def __init__(self):
            self.deleted = []

        def get(self, _key):
            return b"instance:nonce:3"

        def delete(self, key):
            self.deleted.append(key)

    redis = Redis()
    monkeypatch.setattr("app.services.aibot_connection.settings.wecom_aibot_enabled", True)
    connection = AibotConnection(redis_client=redis)
    connection.lease_value = "instance:nonce"
    connection.fencing_token = 3
    connection.release_lease()
    assert redis.deleted == [connection.lease_key]
    assert connection.state.value == "STOPPED"


def _recommendation_claim_db(delivery, *, update_result=1):
    query = MagicMock()
    query.populate_existing.return_value = query
    query.filter.return_value = query
    query.with_for_update.return_value = query
    query.first.return_value = delivery
    query.update.return_value = update_result
    db = MagicMock()
    db.query.return_value = query
    return db, query


def test_recommendation_claim_decrypts_ciphertext_before_aibot_send():
    delivery = SimpleNamespace(
        delivery_id="delivery-1",
        status="pending",
        content_ciphertext=b"encrypted-recommendation",
        attempt_count=0,
        next_attempt_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db, query = _recommendation_claim_db(delivery)
    row = SimpleNamespace(
        recommendation_delivery_id="delivery-1",
        status="pending",
        locked_at=None,
        lease_owner=None,
        fencing_token=None,
        next_attempt_at=None,
    )
    writer = AibotOutboxWriter(transport=MagicMock(), lease_owner="owner", fencing_token=7)

    with patch(
        "app.services.recommendation_delivery_service.decrypt_delivery_body",
        return_value="苏州电子厂推荐正文",
    ) as decrypt:
        assert writer._claim_recommendation_body(db, row, datetime.now()) == "苏州电子厂推荐正文"

    decrypt.assert_called_once_with(delivery)
    update_values = query.update.call_args.args[0]
    assert update_values["status"] == "sending"
    assert update_values["lease_owner"] == "owner"
    assert delivery.status == "pending"  # conditional UPDATE is the source of truth


def test_recommendation_decrypt_failure_is_retryable_and_never_returns_plaintext():
    delivery = SimpleNamespace(
        delivery_id="delivery-1",
        status="pending",
        content_ciphertext=b"bad-ciphertext",
        attempt_count=0,
        next_attempt_at=datetime.now(timezone.utc).replace(tzinfo=None),
        lease_owner="old-owner",
        lease_expires_at="old-expiry",
    )
    db, _query = _recommendation_claim_db(delivery)
    row = SimpleNamespace(
        recommendation_delivery_id="delivery-1",
        status="pending",
        locked_at="claimed",
        lease_owner="owner",
        fencing_token=7,
        next_attempt_at=None,
    )
    writer = AibotOutboxWriter(transport=MagicMock(), lease_owner="owner", fencing_token=7)

    with patch(
        "app.services.recommendation_delivery_service.decrypt_delivery_body",
        side_effect=ValueError("invalid tag"),
    ):
        assert writer._claim_recommendation_body(db, row, datetime.now()) is None

    assert delivery.status == "retry_wait"
    assert delivery.last_error_code == "content_decrypt_failed"
    assert row.status == "pending"
    assert row.lease_owner is None
    assert row.fencing_token is None


def test_recommendation_ack_marks_outbox_and_delivery_sent_atomically():
    outbox_query = MagicMock()
    outbox_query.filter.return_value = outbox_query
    outbox_query.update.return_value = 1
    delivery_query = MagicMock()
    delivery_query.filter.return_value = delivery_query
    delivery_query.update.return_value = 1
    db = MagicMock()
    db.query.side_effect = [outbox_query, delivery_query]
    writer = AibotOutboxWriter(transport=MagicMock(), lease_owner="owner", fencing_token=7)
    item = {
        "id": 188,
        "recommendation_delivery_id": "delivery-1",
        "lease_owner": "owner",
        "fencing_token": 7,
    }
    response = {
        "headers": {"req_id": "req-188"},
        "errcode": 0,
        "errmsg": "ok",
        "msgid": "wx-188",
    }

    with patch("app.services.aibot_connection.SessionLocal", return_value=db):
        assert writer._mark_sent(item, response) is True

    delivery_values = delivery_query.update.call_args.args[0]
    assert delivery_values["status"] == "sent"
    assert delivery_values["wecom_msgid"] == "wx-188"
    assert delivery_values["wecom_response"]["msgid"] == "wx-188"
    assert delivery_values["sent_at"] is not None
    db.commit.assert_called_once()


def test_recommendation_ack_fencing_mismatch_rolls_back_without_partial_sent():
    outbox_query = MagicMock()
    outbox_query.filter.return_value = outbox_query
    outbox_query.update.return_value = 1
    delivery_query = MagicMock()
    delivery_query.filter.return_value = delivery_query
    delivery_query.update.return_value = 0
    db = MagicMock()
    db.query.side_effect = [outbox_query, delivery_query]
    writer = AibotOutboxWriter(transport=MagicMock(), lease_owner="owner", fencing_token=7)

    with patch("app.services.aibot_connection.SessionLocal", return_value=db):
        assert writer._mark_sent(
            {"id": 188, "recommendation_delivery_id": "delivery-1"},
            {"headers": {"req_id": "req-188"}, "errcode": 0, "errmsg": "ok"},
        ) is False

    db.rollback.assert_called_once()
    db.commit.assert_not_called()
