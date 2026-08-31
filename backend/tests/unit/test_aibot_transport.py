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


class SingleRecvSocket(FakeSocket):
    """Fake WSS that fails if two callers wait on recv concurrently."""

    def __init__(self):
        super().__init__()
        self.recv_active = 0
        self.max_recv_active = 0

    async def recv(self):
        self.recv_active += 1
        self.max_recv_active = max(self.max_recv_active, self.recv_active)
        if self.recv_active > 1:
            self.recv_active -= 1
            raise RuntimeError("concurrent recv")
        try:
            return await self.incoming.get()
        finally:
            self.recv_active -= 1


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


@pytest.mark.asyncio
async def test_reader_is_the_only_recv_owner_for_outbound_ack():
    sock = SingleRecvSocket()

    async def send(payload):
        import json
        frame = json.loads(payload)
        sock.sent.append(payload)
        if frame["cmd"] == "aibot_subscribe":
            await sock.incoming.put(json.dumps({
                "headers": {"req_id": frame["headers"]["req_id"]},
                "errcode": 0,
                "errmsg": "ok",
            }))
        elif frame["headers"]["req_id"] == "out-1":
            await sock.incoming.put(json.dumps({
                "headers": {"req_id": "out-1"},
                "errcode": 0,
                "errmsg": "ok",
            }))

    sock.send = send
    transport = AibotTransport(
        AibotClient("BOTID", "SECRET"),
        connect_factory=lambda _url: sock,
        lease_acquire=lambda: (True, 1),
        lease_renew=lambda _token: True,
        heartbeat_seconds=60,
    )
    assert await transport.connect_once()
    result = await transport.send(transport.client.respond_msg("out-1", "hello"), timeout=0.2)
    assert result == (0, "ok")
    assert sock.max_recv_active == 1
    await transport.close()


@pytest.mark.asyncio
async def test_malformed_ack_is_dropped_without_stopping_reader():
    transport = AibotTransport(AibotClient("BOTID", "SECRET"))
    transport.state = TransportState.ACTIVE
    transport.fencing_token = 1
    await transport.handle_frame({"headers": {"req_id": "missing"}, "errcode": None, "errmsg": {}})
    await transport.handle_frame({"cmd": "unknown", "headers": {"req_id": "x"}, "body": {}})
    assert transport.state == TransportState.ACTIVE


def test_state_transition_and_backoff_are_bounded():
    transport = AibotTransport(AibotClient("BOTID", "SECRET"))
    transport.transition(TransportState.ACQUIRING_LEASE)
    with pytest.raises(AibotTransportError):
        transport.transition(TransportState.ACTIVE)
    assert 0 < transport.backoff_delay(20, maximum=3, jitter=0, random_fn=lambda: 0.5) <= 3
