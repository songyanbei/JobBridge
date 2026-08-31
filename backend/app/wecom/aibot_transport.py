"""Single-connection transport and state machine for the AIBot WSS protocol.

The transport is deliberately independent of a particular websocket package.
Tests and the connection service inject a ``connect_factory`` returning an
object with async ``send``, ``recv`` and ``close`` methods.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import random
import uuid
from contextlib import suppress
from enum import Enum
from typing import Any, Awaitable, Callable

from app.wecom.aibot_callback import AibotProtocolError, parse_callback, parse_frame
from app.wecom.aibot_client import AibotClient, AibotClientError

logger = logging.getLogger(__name__)


class TransportState(str, Enum):
    STOPPED = "STOPPED"
    ACQUIRING_LEASE = "ACQUIRING_LEASE"
    CONNECTING = "CONNECTING"
    SUBSCRIBING = "SUBSCRIBING"
    ACTIVE = "ACTIVE"
    DRAINING = "DRAINING"
    BACKOFF = "BACKOFF"


State = TransportState


class AibotTransportError(RuntimeError):
    pass


MaybeAwaitable = Callable[..., Any]


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class AibotTransport:
    """Own one active websocket and fence all writes by the current lease."""

    _VALID_TRANSITIONS = {
        TransportState.STOPPED: {TransportState.ACQUIRING_LEASE},
        TransportState.ACQUIRING_LEASE: {TransportState.CONNECTING, TransportState.BACKOFF, TransportState.STOPPED},
        TransportState.CONNECTING: {TransportState.SUBSCRIBING, TransportState.DRAINING, TransportState.BACKOFF},
        TransportState.SUBSCRIBING: {TransportState.ACTIVE, TransportState.DRAINING, TransportState.BACKOFF},
        TransportState.ACTIVE: {TransportState.DRAINING, TransportState.BACKOFF, TransportState.STOPPED},
        TransportState.DRAINING: {TransportState.BACKOFF, TransportState.STOPPED},
        TransportState.BACKOFF: {TransportState.ACQUIRING_LEASE, TransportState.STOPPED},
    }

    def __init__(
        self,
        client: AibotClient,
        *,
        ws_url: str = "wss://openws.work.weixin.qq.com",
        connect_factory: MaybeAwaitable | None = None,
        lease_acquire: MaybeAwaitable | None = None,
        lease_renew: MaybeAwaitable | None = None,
        lease_release: MaybeAwaitable | None = None,
        instance_id: str = "",
        heartbeat_seconds: float = 30.0,
        connect_timeout: float = 10.0,
        subscribe_timeout: float = 10.0,
        ack_timeout: float = 10.0,
        reconnect_max_seconds: float = 60.0,
        jitter: float = 0.1,
        on_callback: Callable[[Any], Any] | None = None,
    ):
        self.client = client
        self.ws_url = ws_url
        self.connect_factory = connect_factory
        self.lease_acquire = lease_acquire or (lambda: True)
        self.lease_renew = lease_renew or (lambda *_: True)
        self.lease_release = lease_release or (lambda *_: True)
        self.instance_id = instance_id
        self.heartbeat_seconds = heartbeat_seconds
        self.connect_timeout = connect_timeout
        self.subscribe_timeout = subscribe_timeout
        self.ack_timeout = ack_timeout
        self.reconnect_max_seconds = reconnect_max_seconds
        self.jitter = max(0.0, min(float(jitter), 1.0))
        self.on_callback = on_callback
        self.state = TransportState.STOPPED
        self.socket: Any = None
        self.fencing_token: int | str | None = None
        self._stop = asyncio.Event()
        self._ack_waiters: dict[str, asyncio.Future[tuple[int, str]]] = {}
        self._heartbeat_task: asyncio.Task[Any] | None = None
        self._attempt = 0
        self.last_error: str = ""

    def transition(self, new_state: TransportState) -> None:
        new_state = TransportState(new_state)
        if new_state == self.state:
            return
        if new_state not in self._VALID_TRANSITIONS.get(self.state, set()):
            raise AibotTransportError(f"invalid transition {self.state.value}->{new_state.value}")
        self.state = new_state
        logger.info("aibot websocket state=%s", new_state.value)

    @property
    def is_fenced(self) -> bool:
        return self.state == TransportState.ACTIVE and self.fencing_token is not None

    def request_stop(self) -> None:
        self._stop.set()

    @staticmethod
    def backoff_delay(attempt: int, *, maximum: float = 60.0, jitter: float = 0.1, random_fn: Callable[[], float] = random.random) -> float:
        base = min(maximum, 2 ** max(0, int(attempt)))
        return min(maximum, base * (1.0 + max(0.0, min(jitter, 1.0)) * (2 * random_fn() - 1)))

    async def _acquire(self) -> bool:
        result = await _maybe_await(self.lease_acquire())
        if isinstance(result, tuple):
            acquired, token = (result + (None, None))[:2]
            if acquired:
                self.fencing_token = token
            return bool(acquired)
        if isinstance(result, dict):
            acquired = bool(result.get("acquired", result.get("ok", False)))
            if acquired:
                self.fencing_token = result.get("fencing_token", result.get("token"))
            return acquired
        if result:
            self.fencing_token = self.fencing_token or 1
        return bool(result)

    async def _renew(self) -> bool:
        result = await _maybe_await(self.lease_renew(self.fencing_token))
        return bool(result)

    async def _release(self) -> None:
        try:
            await _maybe_await(self.lease_release(self.fencing_token))
        finally:
            self.fencing_token = None

    async def _connect_socket(self) -> Any:
        if self.connect_factory is None:
            try:
                import websockets  # type: ignore
            except ImportError as exc:
                raise AibotTransportError("websockets package is required for the default connector") from exc
            return await websockets.connect(self.ws_url, open_timeout=self.connect_timeout)
        return await _maybe_await(self.connect_factory(self.ws_url))

    async def connect_once(self) -> bool:
        """Acquire lease, subscribe, and enter ACTIVE; errors are recoverable."""
        if self.state == TransportState.STOPPED:
            self.transition(TransportState.ACQUIRING_LEASE)
        if self.state != TransportState.ACQUIRING_LEASE:
            raise AibotTransportError(f"connect_once requires ACQUIRING_LEASE, got {self.state.value}")
        if not await self._acquire():
            self.transition(TransportState.BACKOFF)
            return False
        try:
            self.transition(TransportState.CONNECTING)
            self.socket = await asyncio.wait_for(self._connect_socket(), timeout=self.connect_timeout)
            self.transition(TransportState.SUBSCRIBING)
            subscribe = self.client.subscribe()
            await self._send_raw(subscribe)
            req_id = subscribe["headers"]["req_id"]
            errcode, errmsg = await asyncio.wait_for(self._wait_for_ack(req_id), timeout=self.subscribe_timeout)
            if errcode != 0:
                raise AibotTransportError(f"subscribe rejected errcode={errcode}")
            self.transition(TransportState.ACTIVE)
            self._attempt = 0
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            return True
        except Exception as exc:  # noqa: BLE001 - transport must recover all socket failures
            self.last_error = type(exc).__name__
            logger.warning("aibot websocket connect failed error_type=%s", self.last_error)
            await self.close()
            if self.state not in {TransportState.BACKOFF, TransportState.STOPPED}:
                self.transition(TransportState.BACKOFF)
            return False

    async def _send_raw(self, frame: dict[str, Any]) -> None:
        if self.socket is None:
            raise AibotTransportError("socket is not connected")
        if not self.is_fenced and self.state != TransportState.SUBSCRIBING:
            raise AibotTransportError("connection is not fenced")
        payload = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
        await _maybe_await(self.socket.send(payload))

    async def _wait_for_ack(self, req_id: str) -> tuple[int, str]:
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[tuple[int, str]] = loop.create_future()
        self._ack_waiters[req_id] = waiter
        try:
            while not waiter.done():
                incoming = await _maybe_await(self.socket.recv())
                await self.handle_frame(incoming)
            return await waiter
        finally:
            self._ack_waiters.pop(req_id, None)

    async def send(self, frame: dict[str, Any], *, timeout: float | None = None) -> tuple[int, str]:
        """Send one command and require a matching successful protocol ACK."""
        if self.state != TransportState.ACTIVE or not self.is_fenced:
            raise AibotTransportError("active fenced connection required")
        req_id = ((frame.get("headers") or {}).get("req_id"))
        if not req_id:
            raise AibotClientError("outbound frame missing headers.req_id")
        if not await self._renew():
            await self.lease_lost()
            raise AibotTransportError("lease lost before send")
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[tuple[int, str]] = loop.create_future()
        self._ack_waiters[req_id] = waiter
        try:
            await self._send_raw(frame)
            deadline = self.ack_timeout if timeout is None else timeout
            end = asyncio.get_running_loop().time() + deadline
            while not waiter.done():
                remaining = end - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                recv_task = asyncio.create_task(_maybe_await(self.socket.recv()))
                done, _ = await asyncio.wait({waiter, recv_task}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
                if waiter in done:
                    recv_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await recv_task
                    break
                if recv_task not in done:
                    recv_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await recv_task
                    raise asyncio.TimeoutError
                await self.handle_frame(recv_task.result())
            result = await waiter
            return result
        except asyncio.TimeoutError as exc:
            raise AibotTransportError("ack timeout; delivery is uncertain") from exc
        finally:
            self._ack_waiters.pop(req_id, None)

    async def send_outbox(self, item: dict[str, Any]) -> dict[str, Any]:
        """Build and send the protocol frame represented by an outbox row.

        ``AibotSender`` passes channel-neutral outbox metadata, while
        :meth:`send` intentionally accepts only a validated protocol frame.
        Keeping this adapter here prevents workers from learning WebSocket
        details and gives the synchronous outbox state machine a stable ACK
        mapping to validate.
        """
        command = str(item.get("reply_command") or "aibot_respond_msg")
        req_id = str(item.get("provider_req_id") or item.get("ack_req_id") or f"out-{uuid.uuid4().hex}")
        content = str(item.get("content") or "")
        stream_id = item.get("stream_id")
        finish = bool(item.get("finish", False))
        if command == "aibot_respond_welcome_msg":
            frame = self.client.respond_welcome(req_id, content)
        elif command == "aibot_send_msg":
            frame = self.client.send_msg(req_id, content, chat_id=item.get("chat_id"))
        elif command == "aibot_respond_update_msg":
            frame = self.client.respond_update_msg(req_id, str(stream_id or ""), content, finish=finish)
        elif stream_id:
            frame = self.client.stream(req_id, str(stream_id), content, finish=finish)
        elif command == "aibot_respond_msg":
            frame = self.client.respond_msg(req_id, content)
        else:
            raise AibotClientError(f"unsupported AIBot outbox command: {command}")
        errcode, errmsg = await self.send(frame)
        return {"headers": {"req_id": req_id}, "errcode": errcode, "errmsg": errmsg}

    async def handle_frame(self, payload: Any) -> None:
        """Route ACKs to waiters and callbacks to the reader handler."""
        # ACKs intentionally have no ``cmd`` in the official fixture.
        try:
            value = payload if isinstance(payload, dict) else json.loads(
                payload.decode() if isinstance(payload, bytes) else payload
            )
            if isinstance(value, dict) and "cmd" not in value and isinstance(value.get("headers"), dict):
                req_id = value["headers"].get("req_id")
                waiter = self._ack_waiters.get(req_id)
                if waiter is None or waiter.done():
                    logger.info("aibot websocket orphan ack req_id=%s", req_id)
                    return
                waiter.set_result((int(value.get("errcode", -1)), str(value.get("errmsg", ""))))
                return
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        try:
            frame = parse_frame(payload)
        except AibotProtocolError:
            logger.warning("aibot websocket ignored malformed frame")
            return
        if frame.cmd in {"aibot_msg_callback", "aibot_event_callback"}:
            try:
                callback = parse_callback(payload)
            except AibotProtocolError:
                logger.warning("aibot websocket rejected callback")
                return
            if self.on_callback is not None:
                await _maybe_await(self.on_callback(callback))
            return
        if frame.cmd in {"ping", "pong"}:
            return
        # ACKs have no cmd in the official fixture; parse_frame above is only
        # used for headers/body validation, so also accept raw ack objects.
        value = payload if isinstance(payload, dict) else json.loads(payload.decode() if isinstance(payload, bytes) else payload)
        req_id = ((value.get("headers") or {}).get("req_id"))
        waiter = self._ack_waiters.get(req_id)
        if waiter is None or waiter.done():
            logger.info("aibot websocket orphan ack req_id=%s", req_id)
            return
        waiter.set_result((int(value.get("errcode", -1)), str(value.get("errmsg", ""))))

    async def _heartbeat_loop(self) -> None:
        while self.state == TransportState.ACTIVE and not self._stop.is_set():
            try:
                await asyncio.sleep(self.heartbeat_seconds)
                if self.state != TransportState.ACTIVE:
                    return
                if not await self._renew():
                    await self.lease_lost()
                    return
                await self._send_raw(self.client.ping())
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                self.last_error = type(exc).__name__
                logger.warning("aibot heartbeat failed error_type=%s", self.last_error)
                await self.disconnect()
                return

    async def lease_lost(self) -> None:
        if self.state in {TransportState.ACTIVE, TransportState.SUBSCRIBING, TransportState.CONNECTING}:
            self.transition(TransportState.DRAINING)
        await self.close()

    async def disconnect(self) -> None:
        if self.state not in {TransportState.STOPPED, TransportState.DRAINING}:
            self.transition(TransportState.DRAINING)
        await self.close()
        if self.state == TransportState.DRAINING:
            self.transition(TransportState.BACKOFF)

    async def close(self) -> None:
        if self._heartbeat_task is not None and self._heartbeat_task is not asyncio.current_task():
            self._heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._heartbeat_task
            self._heartbeat_task = None
        socket, self.socket = self.socket, None
        if socket is not None:
            try:
                await _maybe_await(socket.close())
            except Exception:  # noqa: BLE001
                logger.debug("aibot websocket close failed", exc_info=True)
        await self._release()

    async def run(self) -> None:
        """Reconnect until ``request_stop`` is called."""
        self._stop.clear()
        while not self._stop.is_set():
            if self.state == TransportState.STOPPED:
                self.transition(TransportState.ACQUIRING_LEASE)
            if self.state == TransportState.BACKOFF:
                delay = self.backoff_delay(self._attempt, maximum=self.reconnect_max_seconds, jitter=self.jitter)
                self._attempt += 1
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                if self._stop.is_set():
                    break
                self.transition(TransportState.ACQUIRING_LEASE)
            if self.state == TransportState.ACQUIRING_LEASE:
                if not await self.connect_once():
                    continue
            if self.state != TransportState.ACTIVE:
                continue
            try:
                incoming = await _maybe_await(self.socket.recv())
                await self.handle_frame(incoming)
            except Exception as exc:  # noqa: BLE001
                self.last_error = type(exc).__name__
                logger.warning("aibot websocket receive failed error_type=%s", self.last_error)
                await self.disconnect()
        if self.state != TransportState.STOPPED:
            if self.state not in {TransportState.DRAINING}:
                self.transition(TransportState.DRAINING)
            await self.close()
            self.transition(TransportState.STOPPED)


AIBotTransport = AibotTransport
