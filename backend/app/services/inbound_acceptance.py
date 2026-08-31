"""Durable acceptance for legacy WeCom and AIBot inbound messages.

The service owns the DB idempotency record and the queue envelope.  Redis is
only an L1 optimization: a hit is always confirmed against the database.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Literal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.redis_client import check_msg_duplicate, enqueue_message, QUEUE_INCOMING
from app.db import SessionLocal
from app.models import WecomInboundEvent
from app.wecom.callback import WeComMessage

logger = logging.getLogger(__name__)

AcceptanceStatus = Literal["accepted", "duplicate", "retryable", "invalid"]


@dataclass(frozen=True)
class AcceptanceResult:
    status: AcceptanceStatus
    event_id: int | None = None
    payload: dict | None = None
    reason: str | None = None

    @property
    def acknowledged(self) -> bool:
        return self.status in ("accepted", "duplicate")


class InboundAcceptanceService:
    """Persist one message and enqueue its recoverable worker envelope.

    ``strict_redis`` is true for AIBot, where the connector must not send a
    protocol success acknowledgement while infrastructure state is unknown.
    Legacy webhook callers may set it false to retain the historical fail-open
    L1/enqueue behavior; the durable DB row still remains authoritative.
    """

    def __init__(
        self,
        *,
        db_factory: Callable[[], Session] = SessionLocal,
        duplicate_check: Callable[[str], bool] = check_msg_duplicate,
        enqueue: Callable[[str, str], None] = enqueue_message,
    ) -> None:
        self._db_factory = db_factory
        self._duplicate_check = duplicate_check
        self._enqueue = enqueue

    def accept(self, msg: WeComMessage, *, strict_redis: bool | None = None) -> AcceptanceResult:
        is_aibot = msg.source_channel == "wecom_aibot"
        strict = is_aibot if strict_redis is None else strict_redis
        normalized = self._normalize(msg)
        if normalized is None:
            return AcceptanceResult("invalid", reason="invalid inbound message metadata")

        identity = normalized["provider_msg_id"] if is_aibot else normalized["msg_id"]
        try:
            l1_hit = bool(self._duplicate_check(identity))
        except Exception:
            logger.exception("inbound acceptance L1 dedup failed")
            if strict:
                return AcceptanceResult("retryable", reason="redis dedup unavailable")
            l1_hit = False

        try:
            db = self._db_factory()
        except Exception:
            logger.exception("inbound acceptance DB session unavailable identity=%s", identity)
            return AcceptanceResult("retryable", reason="database unavailable")
        try:
            if l1_hit:
                try:
                    existing = self._find_existing(db, normalized, is_aibot)
                except Exception:
                    db.rollback()
                    logger.exception("inbound acceptance durable dedup lookup failed identity=%s", identity)
                    if strict:
                        return AcceptanceResult("retryable", reason="database dedup unavailable")
                    existing = None
                if existing is None:
                    # A Redis marker without a durable row must never drop an
                    # upstream retry.  Continue through the insert path.
                    logger.warning("stale inbound L1 marker identity=%s", identity)
                else:
                    return AcceptanceResult("duplicate", event_id=int(existing.id))

            event = WecomInboundEvent(**self._event_values(normalized, msg))
            db.add(event)
            db.commit()
            db.refresh(event)
            event_id = int(event.id)
            payload = self._queue_payload(event, normalized, msg)
        except IntegrityError:
            db.rollback()
            try:
                existing = self._find_existing(db, normalized, is_aibot)
            except Exception:
                logger.exception("inbound acceptance duplicate lookup failed identity=%s", identity)
                existing = None
            if existing is not None:
                return AcceptanceResult("duplicate", event_id=int(existing.id))
            logger.exception("inbound acceptance integrity failure identity=%s", identity)
            return AcceptanceResult("retryable", reason="database uniqueness check unavailable")
        except Exception:
            db.rollback()
            logger.exception("inbound acceptance DB failure identity=%s", identity)
            return AcceptanceResult("retryable", reason="database unavailable")
        finally:
            db.close()

        try:
            self._enqueue(json.dumps(payload, ensure_ascii=False), QUEUE_INCOMING)
        except Exception:
            logger.exception("inbound acceptance enqueue failed event_id=%s", event_id)
            # Strict AIBot callers must not acknowledge an uncertain queue
            # write.  Recovery will find the durable received row later.
            if strict:
                return AcceptanceResult("retryable", event_id=event_id, payload=payload, reason="redis queue unavailable")
        return AcceptanceResult("accepted", event_id=event_id, payload=payload)

    @staticmethod
    def _normalize(msg: WeComMessage) -> dict | None:
        source = msg.source_channel or "wecom_app"
        if source not in {"wecom_app", "wecom_aibot"}:
            return None
        actor = msg.from_user or ""
        ctype = msg.conversation_type or "single"
        conversation_id = msg.conversation_id or (msg.chat_id if ctype == "group" else actor)
        chat_id = msg.chat_id or (conversation_id if ctype == "group" else "")
        if ctype not in {"single", "group"} or not actor or not conversation_id:
            return None
        if ctype == "group" and not chat_id:
            return None
        # AIBot provider IDs are the protocol's authoritative identity.  The
        # internal ``msg_id`` is derived from it and must never be used as a
        # silent fallback when the provider field is missing.
        provider_id = msg.provider_msg_id if source == "wecom_aibot" else ""
        if source == "wecom_aibot" and not (1 <= len(provider_id) <= 128):
            return None
        if source == "wecom_app" and not msg.msg_id:
            return None
        dedupe = hashlib.sha256(f"{source}\0{provider_id}".encode()).hexdigest() if provider_id else None
        internal_msg_id = (
            "aibot_" + hashlib.sha256(f"{source}\0{provider_id}".encode()).hexdigest()[:58]
            if source == "wecom_aibot" else msg.msg_id
        )
        ordering = msg.ordering_key or f"wecom:{source}:{ctype}:{conversation_id}"
        return {
            "msg_id": internal_msg_id,
            "provider_msg_id": provider_id or None,
            "dedupe_key": dedupe,
            "source_channel": source,
            "from_userid": actor,
            "conversation_type": ctype,
            "conversation_id": conversation_id,
            "chat_id": chat_id or None,
            "ordering_key": ordering,
            "provider_req_id": msg.provider_req_id or None,
            "aibot_id": msg.aibot_id or None,
            "actor_id_kind": msg.actor_id_kind or "plain",
            "turn_id": msg.turn_id or str(uuid.uuid4()),
        }

    @staticmethod
    def _event_values(normalized: dict, msg: WeComMessage) -> dict:
        raw_type = msg.msg_type if msg.msg_type in {
            "text", "image", "voice", "video", "file", "link", "location", "event",
        } else "other"
        content = msg.content or ""
        if raw_type in {"image", "voice", "video", "file"}:
            content = f"[{raw_type}] media_id saved"
        return {
            **normalized,
            "msg_type": raw_type,
            "media_id": msg.media_id or None,
            "content_brief": content[:500] or None,
            "status": "received",
        }

    @staticmethod
    def _queue_payload(event: WecomInboundEvent, normalized: dict, msg: WeComMessage) -> dict:
        raw_type = event.msg_type or "text"
        content = event.content_brief or ""
        if raw_type in {"image", "voice", "video", "file"}:
            content = ""
        return {
            "schema_version": 2,
            "inbound_event_id": int(event.id),
            "msg_id": event.msg_id,
            "provider_msg_id": event.provider_msg_id,
            "turn_id": event.turn_id,
            "source_channel": event.source_channel,
            "from_userid": event.from_userid,
            "conversation_type": event.conversation_type,
            "conversation_id": event.conversation_id,
            "chat_id": event.chat_id,
            "ordering_key": event.ordering_key,
            "msg_type": raw_type,
            "content": content,
            "media_id": event.media_id or "",
            "media_storage_ref": event.media_storage_ref or "",
            "provider_req_id": event.provider_req_id,
            "aibot_id": event.aibot_id,
            "created_at_epoch": int(event.created_at.timestamp()) if event.created_at else int(time.time()),
            "_retry_count": int(event.retry_count or 0),
            "_enqueued_at": time.time(),
        }

    @staticmethod
    def _find_existing(db: Session, normalized: dict, is_aibot: bool):
        query = db.query(WecomInboundEvent)
        if is_aibot:
            return query.filter(
                WecomInboundEvent.source_channel == normalized["source_channel"],
                WecomInboundEvent.provider_msg_id == normalized["provider_msg_id"],
            ).first()
        return query.filter(WecomInboundEvent.msg_id == normalized["msg_id"]).first()
