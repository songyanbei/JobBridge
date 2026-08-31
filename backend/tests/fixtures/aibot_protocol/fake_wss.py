"""Small deterministic in-memory WebSocket-shaped testbed.

This is deliberately not a WebSocket implementation and has no third-party
dependency.  It models the parts tests need from the AIBot connection: two
endpoints, queued JSON frames, configurable subscribe ACK, duplicate/ordered
callbacks, and old-connection eviction when a new connection subscribes.
Production transport tests can adapt it through ``connect()``/``recv()``/
``send()`` without opening a real socket.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from .protocol import load_fixture


class FakeWSSClosed(ConnectionError):
    """Raised when the testbed closes or evicts a connection."""


@dataclass
class FakeWSSScenario:
    """Controls deterministic server behavior for one test run."""

    subscribe_ok: bool = True
    subscribe_errcode: int = 0
    subscribe_errmsg: str = "ok"
    subscribe_ack_delay: float = 0.0
    evict_previous_on_subscribe: bool = True
    close_after_frames: int | None = None
    drop_ack_for: set[str] = field(default_factory=set)
    callback_order: list[str] | None = None


class FakeWSSConnection:
    def __init__(self, server: "FakeWSS", connection_id: int):
        self.server = server
        self.connection_id = connection_id
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self.subscribed = False
        self._sent_count = 0

    async def send(self, frame: str | dict[str, Any]) -> None:
        if self.closed:
            raise FakeWSSClosed("connection is closed")
        payload = json.loads(frame) if isinstance(frame, str) else frame
        self.sent.append(payload)
        self._sent_count += 1
        await self.server._handle_send(self, payload)

    async def recv(self) -> str:
        if self.closed and self.incoming.empty():
            raise FakeWSSClosed("connection is closed")
        frame = await self.incoming.get()
        if frame is None:
            raise FakeWSSClosed("connection is closed")
        return frame

    async def close(self) -> None:
        self.server.close(self)

    async def push(self, frame: str | dict[str, Any]) -> None:
        if self.closed:
            raise FakeWSSClosed("connection is closed")
        payload = json.loads(frame) if isinstance(frame, str) else frame
        await self.incoming.put(json.dumps(payload, ensure_ascii=False))


class FakeWSS:
    """In-memory endpoint with a WebSocket-client-like ``connect`` method."""

    def __init__(self, scenario: FakeWSSScenario | None = None):
        self.scenario = scenario or FakeWSSScenario()
        self.connections: list[FakeWSSConnection] = []
        self.active: FakeWSSConnection | None = None
        self._next_id = 1

    async def connect(self) -> FakeWSSConnection:
        connection = FakeWSSConnection(self, self._next_id)
        self._next_id += 1
        self.connections.append(connection)
        return connection

    def close(self, connection: FakeWSSConnection) -> None:
        if connection.closed:
            return
        connection.closed = True
        connection.incoming.put_nowait(None)
        if self.active is connection:
            self.active = None

    async def _handle_send(self, connection: FakeWSSConnection, payload: dict[str, Any]) -> None:
        if payload.get("cmd") != "aibot_subscribe":
            command = str(payload.get("cmd") or "")
            req_id = ((payload.get("headers") or {}).get("req_id"))
            if req_id in self.scenario.drop_ack_for:
                return
            if command.startswith("aibot_"):
                await connection.push({"headers": {"req_id": req_id}, "errcode": 0, "errmsg": "ok"})
            return

        if self.scenario.subscribe_ack_delay:
            await asyncio.sleep(self.scenario.subscribe_ack_delay)
        req_id = ((payload.get("headers") or {}).get("req_id"))
        ack = {
            "headers": {"req_id": req_id},
            "errcode": 0 if self.scenario.subscribe_ok else self.scenario.subscribe_errcode,
            "errmsg": self.scenario.subscribe_errmsg,
        }
        await connection.push(ack)
        if self.scenario.subscribe_ok:
            if self.scenario.evict_previous_on_subscribe and self.active and self.active is not connection:
                self.close(self.active)
            self.active = connection
            connection.subscribed = True

    async def subscribe(self, connection: FakeWSSConnection | None = None) -> dict[str, Any]:
        """Drive the standard subscribe fixture and return its ACK."""

        connection = connection or await self.connect()
        await connection.send(load_fixture("aibot_subscribe"))
        return json.loads(await connection.recv())

    async def deliver(self, *names: str, duplicate: bool = False) -> None:
        """Push callback fixtures to the active connection in requested order."""

        connection = self.active
        if connection is None or connection.closed:
            raise FakeWSSClosed("no active subscribed connection")
        selected = list(names)
        if self.scenario.callback_order is not None:
            selected = list(self.scenario.callback_order)
        frames: list[dict[str, Any]] = [load_fixture(name) for name in selected]
        if duplicate:
            frames += [dict(frame) for frame in frames]
        for frame in frames:
            await connection.push(frame)

