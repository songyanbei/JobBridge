import asyncio
import json
from types import SimpleNamespace

import pytest

from app.services.aibot_connection import AibotConnection
from app.wecom.aibot_callback import parse_callback
from app.wecom.aibot_client import AibotClient
from app.wecom.aibot_transport import AibotTransport


class EventSocket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []

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
        return None


def _event(event_type="enter_chat"):
    return parse_callback({
        "cmd": "aibot_event_callback",
        "headers": {"req_id": "evt-1"},
        "body": {
            "msgid": "EVENT-1",
            "aibotid": "BOTID",
            "chattype": "single",
            "chatid": "",
            "from": {"userid": "USERID"},
            "msgtype": "event",
            "event": {"eventtype": event_type},
        },
    })


@pytest.mark.asyncio
async def test_enter_chat_sends_welcome_after_durable_acceptance():
    socket = EventSocket()
    transport = AibotTransport(
        AibotClient("BOTID", "SECRET"),
        connect_factory=lambda _url: socket,
        lease_acquire=lambda: (True, 1),
        lease_renew=lambda _token: True,
        heartbeat_seconds=60,
    )
    assert await transport.connect_once()
    connection = AibotConnection(redis_client=SimpleNamespace())
    connection.accept_callback = lambda _callback: SimpleNamespace(acknowledged=True, status="accepted")

    await connection.handle_callback(_event(), transport=transport)

    response = socket.sent[-1]
    assert response["cmd"] == "aibot_respond_welcome_msg"
    assert response["headers"]["req_id"] == "evt-1"
    await transport.close()


@pytest.mark.asyncio
async def test_retryable_or_unknown_event_does_not_send_response(caplog):
    socket = EventSocket()
    transport = AibotTransport(
        AibotClient("BOTID", "SECRET"),
        connect_factory=lambda _url: socket,
        lease_acquire=lambda: (True, 1),
        lease_renew=lambda _token: True,
        heartbeat_seconds=60,
    )
    assert await transport.connect_once()
    connection = AibotConnection(redis_client=SimpleNamespace())
    connection.accept_callback = lambda _callback: SimpleNamespace(acknowledged=False, status="retryable", reason="redis down")

    await connection.handle_callback(_event("click"), transport=transport)

    assert len(socket.sent) == 1  # subscribe only
    assert "not acknowledged" in caplog.text
    await transport.close()


@pytest.mark.asyncio
async def test_duplicate_enter_chat_retries_welcome_after_uncertain_first_delivery():
    socket = EventSocket()
    transport = AibotTransport(
        AibotClient("BOTID", "SECRET"),
        connect_factory=lambda _url: socket,
        lease_acquire=lambda: (True, 1),
        lease_renew=lambda _token: True,
        heartbeat_seconds=60,
    )
    assert await transport.connect_once()
    connection = AibotConnection(redis_client=SimpleNamespace())
    results = iter((
        SimpleNamespace(acknowledged=True, status="accepted"),
        SimpleNamespace(acknowledged=True, status="duplicate"),
    ))
    connection.accept_callback = lambda _callback: next(results)

    await connection.handle_callback(_event(), transport=transport)
    await connection.handle_callback(_event(), transport=transport)

    welcomes = [frame for frame in socket.sent if frame["cmd"] == "aibot_respond_welcome_msg"]
    assert len(welcomes) == 2
    await transport.close()
