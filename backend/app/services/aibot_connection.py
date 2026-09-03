"""Single-active AIBot WebSocket connection and outbox writer.

The connector is a separate process (``python -m app.services.aibot_connection``).
Workers never receive a socket object: they only create channel-tagged outbox
rows.  This module keeps the network transport behind a tiny injected protocol
so unit tests can exercise leases and fencing without a live WSS endpoint.
"""
from __future__ import annotations

import asyncio
import hashlib
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
from app.models import ContactDelivery, RecommendationDelivery, WecomOutboundOutbox
from app.services.delivery_registry import AIBOT_CHANNEL, AibotSender
from app.wecom.aibot_client import AibotClient
from app.wecom.aibot_transport import AibotTransport

logger = logging.getLogger(__name__)

LEASE_TTL_MS = 45_000
LEASE_RENEW_SECONDS = 15
OUTBOX_LEASE_SECONDS = 180
OUTBOX_BATCH_SIZE = 20
EVENT_RESPONSE_TIMEOUT_SECONDS = 5.0
ACCEPTANCE_TIMEOUT_SECONDS = 5.0
WELCOME_RESPONSE_CONTENT = "您好！我是智能助手。"


def stable_aibot_ack_req_id(outbox_id: int | str, provider_req_id: str | None = None) -> str:
    """Return the deterministic protocol ACK key for one active-push row."""
    digest = hashlib.sha256(
        f"{outbox_id}\0{provider_req_id or ''}".encode(),
    ).hexdigest()[:24]
    return f"aibot-send-{outbox_id}-{digest}"


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
                now_utc = datetime.utcnow()
                expired_reply = row.reply_expires_at is not None and row.reply_expires_at <= now_utc
                expired_stream = (
                    row.stream_id is not None
                    and row.stream_deadline_at is not None
                    and row.stream_deadline_at <= now_utc
                )
                if expired_reply or expired_stream:
                    row.status = "dead_letter"
                    row.last_error = "stream_deadline_expired" if expired_stream else "reply_window_expired"
                    continue
                content = row.content or ""
                if row.recommendation_delivery_id:
                    content = self._claim_recommendation_body(db, row, now)
                    if content is None:
                        # The recommendation helper has already persisted a
                        # retry/dead-letter decision in this transaction.
                        continue
                elif row.contact_delivery_id:
                    content = self._claim_contact_body(db, row, now)
                    if content is None:
                        continue
                row.status = "sending"
                command = row.reply_command or "aibot_respond_msg"
                if command == "aibot_send_msg" and not row.ack_req_id:
                    row.ack_req_id = stable_aibot_ack_req_id(row.id, row.provider_req_id)
                row.locked_at = now
                row.lease_owner = self.lease_owner
                row.fencing_token = self.fencing_token
                row.attempt_count = int(row.attempt_count or 0) + 1
                claimed.append({
                    "id": int(row.id),
                    "reply_index": int(row.reply_index),
                    "userid": row.userid,
                    "content": content,
                    "recommendation_delivery_id": row.recommendation_delivery_id,
                    "contact_delivery_id": row.contact_delivery_id,
                    "channel": row.channel,
                    "conversation_type": row.conversation_type,
                    "conversation_id": row.conversation_id,
                    "chat_id": row.chat_id,
                    "ordering_key": row.ordering_key,
                    "provider_req_id": row.provider_req_id,
                    "ack_req_id": row.ack_req_id,
                    "reply_command": command,
                    "stream_id": row.stream_id,
                    "finish": row.finish,
                    "first_sent_at": row.first_sent_at,
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

    @staticmethod
    def _defer_recommendation_row(row: WecomOutboundOutbox, reason: str, *, attempts: int = 1) -> None:
        row.status = "pending"
        row.locked_at = None
        row.lease_owner = None
        row.fencing_token = None
        row.next_attempt_at = func.timestampadd(text("SECOND"), min(600, 30 * max(1, attempts)), func.now(6))
        row.last_error = reason[:1000]

    def _claim_recommendation_body(
        self,
        db: Any,
        row: WecomOutboundOutbox,
        now: Any,
    ) -> str | None:
        """Claim and decrypt one recommendation body before any provider write.

        Recommendation outboxes deliberately keep ``content`` NULL.  The
        encrypted body is owned by ``RecommendationDelivery`` and must be
        decrypted only after the delivery row is locked and conditionally
        moved to ``sending`` under this writer's lease.
        """
        delivery = (
            db.query(RecommendationDelivery)
            .populate_existing()
            .filter(RecommendationDelivery.delivery_id == row.recommendation_delivery_id)
            .with_for_update()
            .first()
        )
        if delivery is None:
            row.status = "dead_letter"
            row.locked_at = None
            row.next_attempt_at = None
            row.last_error = "recommendation delivery missing"
            return None

        status = str(delivery.status or "")
        if status == "prepared":
            self._defer_recommendation_row(row, "recommendation delivery still prepared")
            return None
        if status not in {"pending", "retry_wait"}:
            row.status = "dead_letter"
            row.locked_at = None
            row.next_attempt_at = None
            row.last_error = f"recommendation delivery not sendable: {status}"[:1000]
            return None
        if not delivery.content_ciphertext:
            from app.services.recommendation_delivery_service import purge_delivery_content

            delivery.status = "permanent_failed"
            delivery.last_error_code = "content_unavailable"
            delivery.last_error = "recommendation delivery body unavailable"
            delivery.lease_owner = None
            delivery.lease_expires_at = None
            purge_delivery_content(delivery)
            row.status = "dead_letter"
            row.locked_at = None
            row.next_attempt_at = None
            row.last_error = delivery.last_error
            return None

        try:
            from app.services.recommendation_delivery_service import decrypt_delivery_body

            content = decrypt_delivery_body(delivery)
        except Exception as exc:  # noqa: BLE001 - fail closed, keep retryable
            attempts = int(delivery.attempt_count or 0) + 1
            delivery.status = "retry_wait"
            delivery.attempt_count = attempts
            delivery.next_attempt_at = func.timestampadd(
                text("SECOND"), min(600, 30 * max(1, attempts)), func.now(6),
            )
            delivery.last_error_code = "content_decrypt_failed"
            delivery.last_error = f"recommendation decrypt failed: {type(exc).__name__}"[:500]
            delivery.lease_owner = None
            delivery.lease_expires_at = None
            self._defer_recommendation_row(row, delivery.last_error, attempts=attempts)
            return None

        attempts = int(delivery.attempt_count or 0) + 1
        updated = db.query(RecommendationDelivery).filter(
            RecommendationDelivery.delivery_id == delivery.delivery_id,
            RecommendationDelivery.status.in_(("pending", "retry_wait")),
            RecommendationDelivery.next_attempt_at <= now,
        ).update({
            "status": "sending",
            "attempt_count": attempts,
            "lease_owner": self.lease_owner,
            "lease_expires_at": func.timestampadd(
                text("SECOND"), OUTBOX_LEASE_SECONDS, func.now(6),
            ),
            "last_error": None,
            "last_error_code": None,
        }, synchronize_session=False)
        if updated != 1:
            self._defer_recommendation_row(row, "recommendation delivery claimed elsewhere", attempts=attempts)
            return None
        return content

    def recover_stale(self) -> int:
        """Recover stale claims based on whether a frame was durably written."""
        db = SessionLocal()
        try:
            now = func.now(6)
            stale_before = func.timestampadd(text("SECOND"), -OUTBOX_LEASE_SECONDS, now)
            expired = db.query(WecomOutboundOutbox).filter(
                WecomOutboundOutbox.channel == AIBOT_CHANNEL,
                WecomOutboundOutbox.status == "sending",
                WecomOutboundOutbox.locked_at <= stale_before,
                or_(
                    WecomOutboundOutbox.reply_expires_at <= now,
                    and_(
                        WecomOutboundOutbox.stream_id.isnot(None),
                        WecomOutboundOutbox.stream_deadline_at <= now,
                    ),
                ),
            ).update({
                "status": "dead_letter",
                "uncertain_at": None,
                "locked_at": None,
                "last_error": "reply_window_expired",
                "lease_owner": None,
                "fencing_token": None,
            }, synchronize_session=False)
            pending = db.query(WecomOutboundOutbox).filter(
                WecomOutboundOutbox.channel == AIBOT_CHANNEL,
                WecomOutboundOutbox.status == "sending",
                WecomOutboundOutbox.locked_at <= stale_before,
                WecomOutboundOutbox.first_sent_at.is_(None),
            ).update({
                "status": "pending",
                "locked_at": None,
                "lease_owner": None,
                "fencing_token": None,
                "last_error": "aibot sender crashed before frame write; safe to retry",
            }, synchronize_session=False)
            uncertain = db.query(WecomOutboundOutbox).filter(
                WecomOutboundOutbox.channel == AIBOT_CHANNEL,
                WecomOutboundOutbox.status == "sending",
                WecomOutboundOutbox.locked_at <= stale_before,
                WecomOutboundOutbox.first_sent_at.isnot(None),
            ).update({
                "status": "uncertain",
                "uncertain_at": now,
                "locked_at": None,
                "last_error": "aibot frame written; provider outcome unknown",
                "lease_owner": None,
                "fencing_token": None,
            }, synchronize_session=False)
            # Resolve provider references using only the outbox collation, then
            # update each business table by its own primary-key column. Do not
            # compare the utf8mb4 outbox IDs directly with delivery IDs: the
            # deployed legacy schemas use different collations.
            recommendation_ids = [value[0] for value in db.query(
                WecomOutboundOutbox.recommendation_delivery_id,
            ).filter(
                WecomOutboundOutbox.channel == AIBOT_CHANNEL,
                WecomOutboundOutbox.status == "uncertain",
                WecomOutboundOutbox.recommendation_delivery_id.isnot(None),
            ).all()]
            contact_ids = [value[0] for value in db.query(
                WecomOutboundOutbox.contact_delivery_id,
            ).filter(
                WecomOutboundOutbox.channel == AIBOT_CHANNEL,
                WecomOutboundOutbox.status == "uncertain",
                WecomOutboundOutbox.contact_delivery_id.isnot(None),
            ).all()]
            # Keep the business delivery state aligned with AIBot's ambiguous
            # provider outcome. Without this companion update a failed DB
            # commit after ACK would leave delivery stuck in ``sending`` even
            # after the outbox lease has been fenced to ``uncertain``.
            if recommendation_ids:
                db.query(RecommendationDelivery).filter(
                    RecommendationDelivery.status == "sending",
                    RecommendationDelivery.lease_expires_at <= now,
                    RecommendationDelivery.delivery_id.in_(recommendation_ids),
                ).update({
                    "status": "unknown",
                    "last_error_code": "sending_lease_expired",
                    "last_error": "aibot frame written; provider outcome unknown",
                    "lease_owner": None,
                    "lease_expires_at": None,
                }, synchronize_session=False)
            if contact_ids:
                db.query(ContactDelivery).filter(
                    ContactDelivery.status == "sending",
                    ContactDelivery.delivery_id.in_(contact_ids),
                ).update({"status": "retry_wait"}, synchronize_session=False)
            db.commit()
            return int((pending or 0) + (uncertain or 0) + (expired or 0))
        except Exception:
            db.rollback()
            logger.exception("aibot: stale outbox recovery failed")
            return 0
        finally:
            db.close()

    def deliver(self, item: Mapping[str, Any]) -> bool:
        send_item = dict(item)
        send_item["_on_frame_written"] = lambda: self._mark_frame_written(item)
        try:
            response = self.sender.send(send_item)
        except AibotAckTimeout as exc:
            return self._mark_uncertain(item, str(exc))
        except Exception as exc:
            # A transport that reports a write/ack ambiguity should raise
            # AibotAckTimeout. Other failures happen before a frame is written,
            # so the row remains retryable pending.
            if "ack timeout" in str(exc).lower() and "uncertain" in str(exc).lower():
                return self._mark_uncertain(item, str(exc))
            return self._mark_pending(item, str(exc))

        if not self._valid_ack(response, item):
            return self._mark_uncertain(item, "aibot acknowledgement missing or mismatched")
        return self._mark_sent(item, response)

    async def deliver_async(self, item: Mapping[str, Any]) -> bool:
        """Async counterpart used by the connector owning an async socket."""
        async def mark_frame_written() -> None:
            await asyncio.to_thread(self._mark_frame_written, item)

        send_item = dict(item)
        send_item["_on_frame_written"] = mark_frame_written
        try:
            response = self.sender.send(send_item)
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
        if item.get("reply_command") == "aibot_send_msg":
            req_id = item.get("ack_req_id")
            if not req_id:
                return False
        else:
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
        values = {
            "status": "sent",
            "provider_response": summary or None,
            "provider_msg_id": (str(response.get("msgid") or ""))[:128] or None,
            "ack_req_id": item.get("ack_req_id") or req_id,
            "ack_received_at": func.now(6),
            "first_sent_at": func.coalesce(WecomOutboundOutbox.first_sent_at, func.now(6)),
            "sent_at": func.now(6),
            "locked_at": None,
            "lease_owner": None,
            "fencing_token": None,
            "last_error": None,
        }
        delivery_id = item.get("recommendation_delivery_id")
        contact_delivery_id = item.get("contact_delivery_id")
        if not delivery_id and not contact_delivery_id:
            return self._update(item, values)

        # The provider ACK is the irreversible boundary. Commit the opaque
        # outbox row and its business delivery fact together, fenced by the
        # outbox lease and the delivery state. If either conditional update
        # misses, rollback so stale recovery can record an ambiguous outcome
        # instead of falsely marking only one side as sent.
        db = SessionLocal()
        try:
            outbox_updated = db.query(WecomOutboundOutbox).filter(
                WecomOutboundOutbox.id == int(item["id"]),
                WecomOutboundOutbox.channel == AIBOT_CHANNEL,
                WecomOutboundOutbox.status == "sending",
                WecomOutboundOutbox.lease_owner == self.lease_owner,
                WecomOutboundOutbox.fencing_token == self.fencing_token,
            ).update(values, synchronize_session=False)
            if delivery_id:
                delivery_updated = db.query(RecommendationDelivery).filter(
                    RecommendationDelivery.delivery_id == str(delivery_id),
                    RecommendationDelivery.status == "sending",
                    RecommendationDelivery.lease_owner == self.lease_owner,
                ).update({
                    "status": "sent",
                    "wecom_msgid": (str(response.get("msgid") or ""))[:128] or None,
                    "wecom_response": summary or None,
                    "invalid_recipients": None,
                    "last_error_code": None,
                    "last_error": None,
                    "sent_at": func.now(6),
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "next_attempt_at": func.now(6),
                    "session_patch_ciphertext": None,
                }, synchronize_session=False)
            else:
                delivery_updated = db.query(ContactDelivery).filter(
                    ContactDelivery.delivery_id == str(contact_delivery_id),
                    ContactDelivery.status == "sending",
                ).update({
                    "status": "sent",
                    "sent_at": func.now(6),
                    "revoked_at": None,
                }, synchronize_session=False)
            if outbox_updated != 1 or delivery_updated != 1:
                db.rollback()
                logger.warning(
                    "aibot: sent commit fenced id=%s delivery_id=%s outbox=%s delivery=%s",
                    item.get("id"), delivery_id, outbox_updated, delivery_updated,
                )
                return False
            db.commit()
            return True
        except Exception:
            db.rollback()
            logger.exception("aibot: sent commit failed id=%s", item.get("id"))
            return False
        finally:
            db.close()

    def _claim_contact_body(
        self,
        db: Any,
        row: WecomOutboundOutbox,
        now: Any,
    ) -> str | None:
        """Claim a ContactDelivery and materialize only its safe payload.

        B0 ``platform_request`` is intentionally a fixed, PII-free message;
        actual contact payloads remain fail-closed until their channel-specific
        encrypted delivery adapter is available.
        """
        delivery = (
            db.query(ContactDelivery)
            .populate_existing()
            .filter(ContactDelivery.delivery_id == row.contact_delivery_id)
            .with_for_update()
            .first()
        )
        if delivery is None:
            row.status = "dead_letter"
            row.locked_at = None
            row.next_attempt_at = None
            row.last_error = "contact delivery missing"
            return None
        if delivery.status in {"sent", "revoked", "expired"}:
            row.status = "dead_letter"
            row.locked_at = None
            row.next_attempt_at = None
            row.last_error = f"contact delivery not sendable: {delivery.status}"[:1000]
            return None
        if delivery.expires_at is not None and delivery.expires_at <= datetime.utcnow():
            delivery.status = "expired"
            row.status = "dead_letter"
            row.locked_at = None
            row.next_attempt_at = None
            row.last_error = "contact delivery expired"
            return None
        if delivery.status not in {"prepared", "retry_wait"}:
            self._defer_recommendation_row(row, f"contact delivery not sendable: {delivery.status}")
            return None
        if delivery.channel != "platform_request":
            row.status = "dead_letter"
            row.locked_at = None
            row.next_attempt_at = None
            row.last_error = "unsupported contact delivery channel"
            return None

        from app.listing.contact import CONTACT_PLATFORM_REQUEST_MESSAGE

        delivery.status = "sending"
        return CONTACT_PLATFORM_REQUEST_MESSAGE

    def _mark_frame_written(self, item: Mapping[str, Any]) -> bool:
        values: dict[str, Any] = {
            "first_sent_at": func.coalesce(WecomOutboundOutbox.first_sent_at, func.now(6)),
        }
        if item.get("stream_id"):
            # Officially the ten-minute stream window starts at the first
            # provider write, not at inbound acceptance or outbox creation.
            values["stream_deadline_at"] = func.coalesce(
                WecomOutboundOutbox.stream_deadline_at,
                func.timestampadd(text("SECOND"), 600, func.now(6)),
            )
        return self._update(item, values)

    def _mark_uncertain(self, item: Mapping[str, Any], reason: str) -> bool:
        contact_delivery_id = item.get("contact_delivery_id")
        if contact_delivery_id:
            db = SessionLocal()
            try:
                outbox_updated = db.query(WecomOutboundOutbox).filter(
                    WecomOutboundOutbox.id == int(item["id"]),
                    WecomOutboundOutbox.channel == AIBOT_CHANNEL,
                    WecomOutboundOutbox.status == "sending",
                    WecomOutboundOutbox.lease_owner == self.lease_owner,
                    WecomOutboundOutbox.fencing_token == self.fencing_token,
                ).update({
                    "status": "uncertain",
                    "uncertain_at": func.now(6),
                    "locked_at": None,
                    "lease_owner": None,
                    "fencing_token": None,
                    "last_error": reason[:1000],
                }, synchronize_session=False)
                delivery_updated = db.query(ContactDelivery).filter(
                    ContactDelivery.delivery_id == str(contact_delivery_id),
                    ContactDelivery.status == "sending",
                ).update({"status": "retry_wait"}, synchronize_session=False)
                if outbox_updated != 1 or delivery_updated != 1:
                    db.rollback()
                    return False
                db.commit()
                return True
            except Exception:
                db.rollback()
                logger.exception("aibot: contact uncertain update failed id=%s", item.get("id"))
                return False
            finally:
                db.close()
        return self._update(item, {
            "status": "uncertain",
            "uncertain_at": func.now(6),
            "locked_at": None,
            "lease_owner": None,
            "fencing_token": None,
            "last_error": reason[:1000],
        })

    def _mark_pending(self, item: Mapping[str, Any], reason: str) -> bool:
        contact_delivery_id = item.get("contact_delivery_id")
        if contact_delivery_id:
            db = SessionLocal()
            try:
                outbox_updated = db.query(WecomOutboundOutbox).filter(
                    WecomOutboundOutbox.id == int(item["id"]),
                    WecomOutboundOutbox.channel == AIBOT_CHANNEL,
                    WecomOutboundOutbox.status == "sending",
                    WecomOutboundOutbox.lease_owner == self.lease_owner,
                    WecomOutboundOutbox.fencing_token == self.fencing_token,
                ).update({
                    "status": "pending",
                    "locked_at": None,
                    "lease_owner": None,
                    "fencing_token": None,
                    "next_attempt_at": func.timestampadd(text("SECOND"), 30, func.now(6)),
                    "last_error": reason[:1000],
                }, synchronize_session=False)
                delivery_updated = db.query(ContactDelivery).filter(
                    ContactDelivery.delivery_id == str(contact_delivery_id),
                    ContactDelivery.status == "sending",
                ).update({"status": "retry_wait"}, synchronize_session=False)
                if outbox_updated != 1 or delivery_updated != 1:
                    db.rollback()
                    return False
                db.commit()
                return True
            except Exception:
                db.rollback()
                logger.exception("aibot: contact pending update failed id=%s", item.get("id"))
                return False
            finally:
                db.close()
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
        from app.services.inbound_acceptance import AcceptanceResult

        deadline = asyncio.get_running_loop().time() + min(EVENT_RESPONSE_TIMEOUT_SECONDS, ACCEPTANCE_TIMEOUT_SECONDS)
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self.accept_callback, callback),
                timeout=max(0.0, deadline - asyncio.get_running_loop().time()),
            )
        except asyncio.TimeoutError:
            logger.warning("aibot callback acceptance timed out")
            return AcceptanceResult("retryable", reason="acceptance timeout")
        if not getattr(result, "acknowledged", False):
            logger.warning("aibot callback not acknowledged status=%s reason=%s", getattr(result, "status", "unknown"), getattr(result, "reason", ""))
            return result
        # A duplicate enter_chat can be a provider retry after the first
        # welcome write failed or its ACK was lost. The durable inbox cannot
        # infer whether the protocol response reached the user, so replay the
        # welcome on this narrow event path instead of silently dropping it.
        duplicate_enter = (
            getattr(result, "status", "") == "duplicate"
            and getattr(callback, "command", "") == "aibot_event_callback"
            and str(getattr(callback, "event_type", "") or "") == "enter_chat"
        )
        if getattr(result, "status", "") != "accepted" and not duplicate_enter:
            return result
        if getattr(callback, "command", "") != "aibot_event_callback":
            return result
        event_type = str(getattr(callback, "event_type", "") or "")
        if event_type != "enter_chat":
            logger.warning("aibot event response unsupported event_type=%s; fail-closed", event_type)
            return result
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            logger.warning("aibot enter_chat welcome deadline exceeded")
            return result
        frame = transport.client.respond_welcome(callback.req_id, WELCOME_RESPONSE_CONTENT)
        try:
            await transport.send(frame, timeout=remaining)
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
