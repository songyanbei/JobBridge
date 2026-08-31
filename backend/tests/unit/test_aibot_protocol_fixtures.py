"""Contract checks for the frozen AIBot protocol examples and fake WSS."""

from __future__ import annotations

import asyncio
import json

from tests.fixtures.aibot_protocol import (
    FakeWSS,
    FakeWSSScenario,
    assert_protocol_fixture,
    fixture_names,
    load_fixture,
)


def test_official_fixtures_are_complete_and_field_stable():
    assert set(fixture_names()) == {
        "aibot_subscribe",
        "subscribe_ack",
        "aibot_msg_callback",
        "aibot_event_callback",
        "aibot_respond_welcome_msg",
        "aibot_respond_msg",
        "aibot_respond_update_msg",
        "aibot_send_msg",
    }
    for name in fixture_names():
        assert_protocol_fixture(name)


def test_fake_wss_subscribe_ack_and_old_connection_eviction():
    async def scenario():
        server = FakeWSS()
        first = await server.connect()
        ack = await server.subscribe(first)
        assert ack == load_fixture("subscribe_ack")
        second = await server.connect()
        await server.subscribe(second)
        assert first.closed is True
        assert server.active is second

    asyncio.run(scenario())


def test_fake_wss_can_drop_ack_and_replay_callbacks_in_order():
    async def scenario():
        server = FakeWSS(FakeWSSScenario(drop_ack_for={"missing-1"}))
        connection = await server.connect()
        await server.subscribe(connection)
        await server.deliver("aibot_msg_callback", "aibot_event_callback", duplicate=True)
        frames = [await connection.recv() for _ in range(4)]
        assert [frame["headers"]["req_id"] for frame in map(json.loads, frames)] == [
            "msg-1", "evt-1", "msg-1", "evt-1",
        ]
        await connection.send(load_fixture("aibot_respond_msg"))
        # ACK loss is scoped to the configured req_id; other response ACKs remain available.
        ack = json.loads(await connection.recv())
        assert ack["headers"]["req_id"] == "msg-1"

    asyncio.run(scenario())
