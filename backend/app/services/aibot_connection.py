"""Single-active AIBot WebSocket connection and outbox writer.

The connector is a separate process (``python -m app.services.aibot_connection``).
Workers never receive a socket object: they only create channel-tagged outbox
rows.  This module keeps the network transport behind a tiny injected protocol
so unit tests can exercise leases and fencing without a live WSS endpoint.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping

from sqlalchemy import and_, exists, func, or_, text
from sqlalchemy.orm import aliased

from app.config import settings
from app.core.redis_client import get_redis
from app.db import SessionLocal
from app.models import WecomOutboundOutbox
from app.services.delivery_registry import AIBOT_CHANNEL, AibotSender
from app.wecom.aibot_client import AibotClient
from app.wecom.aibot_transport import AibotTransport

logger = logging.getLogger(__name__)

LEASE_TTL_MS = 45_000
LEASE_RENEW_SECONDS = 15
OUTBOX_LEASE_SECONDS = 180
OUTBOX_BATCH_SIZE = 20
EVENT_RESPONSE_TIMEOUT_SECONDS = 5.0
WELCOME_RESPONSE_CONTENT = "您好！我是智能助手。"


class ConnectionState(StrEnum):
    STOPPED = "STOPPED"
    ACQUIRING_LEASE = "ACQUIRING_LEASE"
    CONNECTING = "CONNECTING"
    SUBSCRIBING = "SUBSCRIBING"
    ACTIVE = "ACTIVE"
    DRAINING = "DRAINING"
    BACKOFF = "BACKOFF"


class AibotAckTimeout(TimeoutError):
    """A frame was written but its matching protocol acknowledgement was lost."""


class AibotOutboxWriter:
    """Claim and deliver only ``channel=wecom_aibot`` rows."""

    def __init__(self, *, transport: Any, lease_owner: str, fencing_token: int):
        self.transport = transport
        self.lease_owner = lease_owner
        self.fencing_token = int(fencing_token)
        self.sender = AibotSender(transport)

    def claim(self, *, limit: int = OUTBOX_BATCH_SIZE) -> list[dict]:
        db = SessionLocal()
        try:
            now = func.now(6)
            stale_before = func.timestampadd(text("SECOND"), -OUTBOX_LEASE_SECONDS, now)
            earlier = aliased(WecomOutboundOutbox)
            rows = (
                db.query(WecomOutboundOutbox)
                .filter(
                    WecomOutboundOutbox.channel == AIBOT_CHANNEL,
                    WecomOutboundOutbox.status == "pending",
                    or_(
                        WecomOutboundOutbox.ordering_key.is_(None),
                        ~exists().where(and_(
                            earlier.ordering_key == WecomOutboundOutbox.ordering_key,
                            earlier.id < WecomOutboundOutbox.id,
                            earlier.status.in_(("pending", "sending")),
                        )),
                    ),
                    or_(
                        WecomOutboundOutbox.next_attempt_at.is_(None),
                        WecomOutboundOutbox.next_attempt_at <= now,
                    ),
                )
                .order_by(WecomOutboundOutbox.id)
                .with_for_update(skip_locked=True)
                .limit(limit)
                .all()
            )
            claimed = []
            for row in rows:
                if row.reply_expires_at is not None and row.reply_expires_at <= datetime.utcnow():
                    row.status = "dead_letter"
                    row.last_error = "reply_window_expired"
                    continue
                row.status = "sending"
                row.locked_at = now
                row.lease_owner = self.lease_owner
                row.fencing_token = self.fencing_token
                row.attempt_count = int(row.attempt_count or 0) + 1
                claimed.append({
                    "id": int(row.id),
                    "userid": row.userid,
                    "content": row.content or "",
                    "channel": row.channel,
                    "conversation_type": row.conversation_type,
                    "conversation_id": row.conversation_id,
                    "chat_id": row.chat_id,
                    "ordering_key": row.ordering_key,
                    "provider_req_id": row.provider_req_id,
                    "reply_command": row.reply_command or "aibot_respond_msg",
                    "stream_id": row.stream_id,
                    "finish": row.finish,
                    "attempt_count": int(row.attempt_count),
                    "lease_owner": self.lease_owner,
                    "fencing_token": self.fencing_token,
                })
            db.commit()
            return claimed
        except Exception:
            db.rollback()
            logger.exception("aibot: outbox claim failed")
            return []
        finally:
            db.close()

    def recover_stale(self) -> int:
        """Fence stale AIBot claims into ``uncertain``; never auto-resend them."""
        db = SessionLocal()
        try:
            now = func.now(6)
            stale_before = func.timestampadd(text("SECOND"), -OUTBOX_LEASE_SECONDS, now)
            count = db.query(WecomOutboundOutbox).filter(
                WecomOutboundOutbox.channel == AIBOT_CHANNEL,
                WecomOutboundOutbox.status == "sending",
                WecomOutboundOutbox.locked_at <= stale_before,
            ).update({
                "status": "uncertain",
                "uncertain_at": now,
                "locked_at": None,
                "last_error": "aibot sending lease expired; provider outcome unknown",
                "lease_owner": None,
                "fencing_token": None,
            }, synchronize_session=False)
            db.commit()
            return int(count or 0)
        except Exception:
            db.rollback()
            logger.exception("aibot: stale outbox recovery failed")
            return 0
        finally:
            db.close()

    def deliver(self, item: Mapping[str, Any]) -> bool:
        try:
            response = self.sender.send(item)
        except AibotAckTimeout as exc:
            return self._mark_uncertain(item, str(exc))
        except Exception as exc:
            # A transport that reports a write/ack ambiguity should raise
            # AibotAckTimeout. Other failures happen before a frame is written,
            # so the row remains retryable pending.
            return self._mark_pending(item, str(exc))

        if not self._valid_ack(response, item):
            return self._mark_uncertain(item, "aibot acknowledgement missing or mismatched")
        return self._mark_sent(item, response)

    async def deliver_async(self, item: Mapping[str, Any]) -> bool:
        """Async counterpart used by the connector owning an async socket."""
        try:
            response = self.sender.send(item)
            if inspect.isawaitable(response):
                response = await response
        except AibotAckTimeout as exc:
            return self._mark_uncertain(item, str(exc))
        except Exception as exc:
            if "ack timeout" in str(exc).lower() and "uncertain" in str(exc).lower():
                return self._mark_uncertain(item, str(exc))
            return self._mark_pending(item, str(exc))
        if not self._valid_ack(response, item):
            return self._mark_uncertain(item, "aibot acknowledgement missing or mismatched")
        return self._mark_sent(item, response)

    @staticmethod
    def _valid_ack(response: Any, item: Mapping[str, Any]) -> bool:
        if not isinstance(response, Mapping) or response.get("errcode") != 0:
            return False
        req_id = item.get("provider_req_id") or item.get("ack_req_id")
        response_req_id = response.get("req_id") or (response.get("headers") or {}).get("req_id")
        return (not req_id and not response_req_id) or (
            bool(req_id) and bool(response_req_id) and str(req_id) == str(response_req_id)
        )

    def _update(self, item: Mapping[str, Any], values: dict[str, Any]) -> bool:
        db = SessionLocal()
        try:
            updated = db.query(WecomOutboundOutbox).filter(
                WecomOutboundOutbox.id == int(item["id"]),
                WecomOutboundOutbox.channel == AIBOT_CHANNEL,
                WecomOutboundOutbox.status == "sending",
                WecomOutboundOutbox.lease_owner == self.lease_owner,
                WecomOutboundOutbox.fencing_token == self.fencing_token,
            ).update(values, synchronize_session=False)
            db.commit()
            return updated == 1
        except Exception:
            db.rollback()
            logger.exception("aibot: outbox state update failed id=%s", item.get("id"))
            return False
        finally:
            db.close()

    def _mark_sent(self, item: Mapping[str, Any], response: Mapping[str, Any]) -> bool:
        headers = response.get("headers") if isinstance(response, Mapping) else {}
        req_id = (headers or {}).get("req_id") if isinstance(headers, Mapping) else None
        summary = {
            key: response[key] for key in ("errcode", "errmsg", "msgid", "response_code")
            if key in response
        }
        return self._update(item, {
            "status": "sent",
            "provider_response": summary or None,
            "ack_req_id": req_id,
            "ack_received_at": func.now(6),
            "first_sent_at": func.coalesce(WecomOutboundOutbox.first_sent_at, func.now(6)),
            "sent_at": func.now(6),
            "locked_at": None,
            "lease_owner": None,
            "fencing_token": None,
            "last_error": None,
        })

    def _mark_uncertain(self, item: Mapping[str, Any], reason: str) -> bool:
        return self._update(item, {
            "status": "uncertain",
            "uncertain_at": func.now(6),
            "locked_at": None,
            "lease_owner": None,
            "fencing_token": None,
            "last_error": reason[:1000],
        })

    def _mark_pending(self, item: Mapping[str, Any], reason: str) -> bool:
        return self._update(item, {
            "status": "pending",
            "locked_at": None,
            "lease_owner": None,
            "fencing_token": None,
            "next_attempt_at": func.timestampadd(text("SECOND"), 30, func.now(6)),
            "last_error": reason[:1000],
        })


class AibotConnection:
    """Redis-leased lifecycle for one bot instance."""

    def __init__(self, *, redis_client: Any | None = None, transport: Any | None = None):
        self.redis = redis_client or get_redis()
        self.transport = transport
        self.bot_id = settings.wecom_aibot_bot_id
        self.instance_id = settings.wecom_aibot_instance_id or f"aibot-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.lease_key = f"wecom:aibot:leader:{self.bot_id}"
        self.lease_value = f"{self.instance_id}:{uuid.uuid4().hex}"
        self.fencing_token: int | None = None
        self.state = ConnectionState.STOPPED
        self.last_error = ""

    def acquire_lease(self) -> bool:
        if not settings.wecom_aibot_enabled:
            self.state = ConnectionState.STOPPED
            return False
        self.state = ConnectionState.ACQUIRING_LEASE
        try:
            token = int(self.redis.incr(f"{self.lease_key}:fencing"))
            if not self.redis.set(self.lease_key, f"{self.lease_value}:{token}", nx=True, px=LEASE_TTL_MS):
                self.state = ConnectionState.BACKOFF
                return False
            self.fencing_token = token
            return True
        except Exception as exc:
            self.last_error = str(exc)
            self.state = ConnectionState.BACKOFF
            return False

    def renew_lease(self) -> bool:
        if self.fencing_token is None:
            return False
        expected = f"{self.lease_value}:{self.fencing_token}"
        try:
            current = self.redis.get(self.lease_key)
            if isinstance(current, bytes):
                current = current.decode()
            if current != expected:
                self.state = ConnectionState.DRAINING
                return False
            self.redis.pexpire(self.lease_key, LEASE_TTL_MS)
            return True
        except Exception as exc:
            self.last_error = str(exc)
            self.state = ConnectionState.DRAINING
            return False

    def release_lease(self) -> None:
        expected = f"{self.lease_value}:{self.fencing_token}" if self.fencing_token is not None else None
        try:
            if expected is not None:
                # Compare-and-delete must be atomic: a plain GET followed by
                # DELETE can erase a replacement owner's newly acquired lease.
                script = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
                try:
                    self.redis.eval(script, 1, self.lease_key, expected)
                except (AttributeError, NotImplementedError):
                    current = self.redis.get(self.lease_key)
                    if isinstance(current, bytes):
                        current = current.decode()
                    if current == expected:
                        self.redis.delete(self.lease_key)
        except Exception:
            logger.warning("aibot: failed to release lease", exc_info=True)
        self.fencing_token = None
        self.state = ConnectionState.STOPPED

    def writer(self) -> AibotOutboxWriter | None:
        transport_fenced = bool(getattr(self.transport, "is_fenced", False))
        if self.transport is None or self.fencing_token is None:
            return None
        if self.state != ConnectionState.ACTIVE and not transport_fenced:
            return None
        token = getattr(self.transport, "fencing_token", None) or self.fencing_token
        return AibotOutboxWriter(
            transport=self.transport,
            lease_owner=self.instance_id,
            fencing_token=int(token),
        )

    def run_once(self) -> int:
        """Process one bounded writer pass; socket read loop is transport-owned."""
        if not self.renew_lease():
            return 0
        writer = self.writer()
        if writer is None:
            return 0
        writer.recover_stale()
        count = 0
        for item in writer.claim():
            writer.deliver(item)
            count += 1
        return count

    async def run_once_async(self) -> int:
        """Process one writer pass without blocking the Reader event loop."""
        writer = self.writer()
        if writer is None:
            return 0
        await asyncio.to_thread(writer.recover_stale)
        items = await asyncio.to_thread(writer.claim)
        for item in items:
            await writer.deliver_async(item)
        return len(items)

    def accept_callback(self, callback: Any) -> Any:
        """Persist a validated callback; ACK policy stays with the Reader."""
        from app.services.inbound_acceptance import InboundAcceptanceService

        return InboundAcceptanceService().accept(callback.to_wecom_message(), strict_redis=True)

    async def handle_callback(self, callback: Any, *, transport: AibotTransport) -> Any:
        """Durably accept a callback and answer only known event commands.

        A retryable DB/Redis result deliberately produces no response frame.
        ``enter_chat`` is the only event response whose command semantics are
        defined locally; unknown card events are logged and ignored rather
        than guessed into a potentially invalid protocol command.
        """
        result = await asyncio.to_thread(self.accept_callback, callback)
        if not getattr(result, "acknowledged", False):
            logger.warning("aibot callback not acknowledged status=%s reason=%s", getattr(result, "status", "unknown"), getattr(result, "reason", ""))
            return result
        if getattr(callback, "command", "") != "aibot_event_callback":
            return result
        event_type = str(getattr(callback, "event_type", "") or "")
        if event_type != "enter_chat":
            logger.warning("aibot event response unsupported event_type=%s; fail-closed", event_type)
            return result
        frame = transport.client.respond_welcome(callback.req_id, WELCOME_RESPONSE_CONTENT)
        try:
            await transport.send(frame, timeout=EVENT_RESPONSE_TIMEOUT_SECONDS)
        except Exception:
            logger.exception("aibot event response failed event_type=%s req_id=%s", event_type, callback.req_id)
        return result


async def _run_connector(connection: AibotConnection) -> None:
    client = AibotClient(settings.wecom_aibot_bot_id, settings.wecom_aibot_secret.get_secret_value())

    def acquire():
        acquired = connection.acquire_lease()
        return acquired, connection.fencing_token

    transport = AibotTransport(
        client,
        ws_url=settings.wecom_aibot_ws_url,
        connect_timeout=settings.wecom_aibot_connect_timeout_seconds,
        subscribe_timeout=settings.wecom_aibot_subscribe_timeout_seconds,
        heartbeat_seconds=settings.wecom_aibot_heartbeat_seconds,
        reconnect_max_seconds=settings.wecom_aibot_reconnect_max_seconds,
        instance_id=connection.instance_id,
        lease_acquire=acquire,
        lease_renew=lambda _token: connection.renew_lease(),
        lease_release=lambda _token: connection.release_lease(),
    )
    connection.transport = transport

    async def on_callback(callback):
        return await connection.handle_callback(callback, transport=transport)

    transport.on_callback = on_callback
    reader = asyncio.create_task(transport.run())
    try:
        while not transport._stop.is_set():
            await connection.run_once_async()
            await asyncio.sleep(1)
    finally:
        transport.request_stop()
        await reader


def main() -> None:
    """Standalone connector entry point owning Reader, Writer and lease."""
    if not settings.wecom_aibot_enabled:
        logger.info("aibot connector disabled")
        return
    connection = AibotConnection()
    asyncio.run(_run_connector(connection))


if __name__ == "__main__":
    main()
