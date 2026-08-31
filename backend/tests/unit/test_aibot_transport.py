import asyncio

import pytest

from app.wecom.aibot_client import AibotClient
from app.wecom.aibot_transport import AibotTransport, AibotTransportError, TransportState


class FakeSocket:
    def __init__(self):
        self.sent = []
        self.incoming = asyncio.Queue()
        self.closed = False

    async def send(self, payload):
        self.sent.append(payload)
        if '"aibot_subscribe"' in payload:
            await self.incoming.put('{"headers":{"req_id":"sub-ack"},"errcode":0,"errmsg":"ok"}')

    async def recv(self):
        return await self.incoming.get()

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_connect_subscribe_and_heartbeat_state_machine():
    sock = FakeSocket()
    transport = AibotTransport(AibotClient("BOTID", "SECRET"), connect_factory=lambda _: sock, heartbeat_seconds=60, subscribe_timeout=0.01)
    assert await transport.connect_once() is False  # fixture ack req_id intentionally does not match generated req_id
    assert transport.state == TransportState.BACKOFF


@pytest.mark.asyncio
async def test_matching_ack_and_lease_fencing():
    sock = FakeSocket()
    async def factory(_):
        return sock
    transport = AibotTransport(AibotClient("BOTID", "SECRET"), connect_factory=factory, lease_renew=lambda _: True)
    # Make subscribe ACK match the generated request ID.
    original_send = sock.send
    async def send(payload):
        await original_send(payload)
        import json
        frame = json.loads(payload)
        if frame["cmd"] == "aibot_subscribe":
            await sock.incoming.put(json.dumps({"headers": {"req_id": frame["headers"]["req_id"]}, "errcode": 0, "errmsg": "ok"}))
    sock.send = send
    assert await transport.connect_once() is True
    assert transport.state == TransportState.ACTIVE
    frame = transport.client.respond_msg("r1", "ok")
    pending = asyncio.create_task(transport.send(frame, timeout=0.2))
    await asyncio.sleep(0)
    await sock.incoming.put('{"headers":{"req_id":"r1"},"errcode":0,"errmsg":"ok"}')
    assert await pending == (0, "ok")
    await transport.close()


def test_state_transition_and_backoff_are_bounded():
    transport = AibotTransport(AibotClient("BOTID", "SECRET"))
    transport.transition(TransportState.ACQUIRING_LEASE)
    with pytest.raises(AibotTransportError):
        transport.transition(TransportState.ACTIVE)
    assert 0 < transport.backoff_delay(20, maximum=3, jitter=0, random_fn=lambda: 0.5) <= 3
