import asyncio
import json

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
        "content": "hello",
        "provider_req_id": "req-1",
        "reply_command": "aibot_respond_msg",
    })

    assert result == {"headers": {"req_id": "req-1"}, "errcode": 0, "errmsg": "ok"}
    assert socket.sent[-1]["cmd"] == "aibot_respond_msg"
    assert socket.sent[-1]["headers"]["req_id"] == "req-1"
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
