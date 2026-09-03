import asyncio

import pytest

from app.wecom.aibot_client import AibotClient
from app.wecom.aibot_transport import AibotTransport, AibotTransportError


@pytest.mark.asyncio
async def test_close_wakes_pending_ack_waiters_as_uncertain():
    transport = AibotTransport(AibotClient("BOTID", "SECRET"))
    waiter = asyncio.get_running_loop().create_future()
    transport._ack_waiters["req-1"] = waiter

    await transport.close()

    assert transport._ack_waiters == {}
    with pytest.raises(AibotTransportError, match="delivery is uncertain"):
        await waiter
