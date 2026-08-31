import asyncio
import threading

import pytest

from app.services.aibot_connection import AibotConnection
from app.services.inbound_acceptance import AcceptanceResult
from app.wecom.aibot_callback import parse_callback
from app.wecom.aibot_client import AibotClient
from app.wecom.aibot_transport import AibotTransport, TransportState


def _callback():
    return parse_callback({
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "msg-1"},
        "body": {
            "msgid": "MSG-1", "aibotid": "BOTID", "chattype": "single",
            "chatid": "",
            "from": {"userid": "USERID"}, "msgtype": "text",
            "text": {"content": "hello"},
        },
    })


@pytest.mark.asyncio
async def test_permanently_blocked_acceptance_times_out_without_success_ack(monkeypatch):
    release = threading.Event()
    connection = AibotConnection(redis_client=object())

    def blocked(_callback):
        release.wait(2)
        return AcceptanceResult("accepted")

    connection.accept_callback = blocked
    monkeypatch.setattr("app.services.aibot_connection.ACCEPTANCE_TIMEOUT_SECONDS", 0.01)
    result = await connection.handle_callback(_callback(), transport=None)
    release.set()

    assert result.status == "retryable"
    assert not result.acknowledged


@pytest.mark.asyncio
async def test_transport_callback_task_does_not_block_reader_frame_dispatch():
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_callback(_callback):
        started.set()
        await release.wait()

    transport = AibotTransport(AibotClient("BOTID", "SECRET"), on_callback=blocked_callback)
    transport.state = TransportState.ACTIVE
    await asyncio.wait_for(transport.handle_frame({
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "msg-1"},
        "body": {
            "msgid": "MSG-1", "aibotid": "BOTID", "chattype": "single",
            "chatid": "",
            "from": {"userid": "USERID"}, "msgtype": "text",
            "text": {"content": "hello"},
        },
    }), timeout=0.05)
    await asyncio.wait_for(started.wait(), timeout=0.05)
    assert transport._callback_tasks
    release.set()
    await transport.close()
