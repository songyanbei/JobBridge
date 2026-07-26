"""Transactional recommendation delivery and privacy helpers.

Recommendation bodies are intentionally kept out of the outbox and conversation
log.  Only this short-lived encrypted envelope is persisted.
"""
from __future__ import annotations

import base64
import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy.orm import Session

from app.models import (
    RecommendationDelivery,
    RecommendationRequest,
    RecommendationSearchAttempt,
    WecomOutboundOutbox,
)


def _key() -> bytes:
    raw = os.getenv("RECOMMENDATION_CONTENT_KEY")
    environment = os.getenv("APP_ENV", "development").lower()
    if not raw and environment in {"production", "prod", "staging"}:
        raise RuntimeError("RECOMMENDATION_CONTENT_KEY is required outside development")
    raw = raw or "jobbridge-recommendation-local-only"
    return hashlib.sha256(raw.encode("utf-8")).digest()


def encrypt_body(body: str) -> str:
    nonce = os.urandom(12)
    ciphertext = AESGCM(_key()).encrypt(nonce, body.encode("utf-8"), None)
    return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")


def decrypt_body(token: str) -> str:
    raw = base64.urlsafe_b64decode(token.encode("ascii"))
    return AESGCM(_key()).decrypt(raw[:12], raw[12:], None).decode("utf-8")


def prepare_delivery(
    db: Session,
    *,
    inbound_event_id: int,
    reply_index: int,
    userid: str,
    body: str,
    request_id: str | None = None,
    snapshot_id: str | None = None,
    position_count: int = 0,
    delivery_id: str | None = None,
    recommendation_context: dict | None = None,
    source_inbound_msg_id: str | None = None,
    request_fact: dict | None = None,
    ttl_minutes: int = 30,
) -> RecommendationDelivery:
    ctx = dict(recommendation_context or {})
    fact = dict(request_fact or {})
    items = list(ctx.get("items") or [])
    candidate_ids = [str(item.get("target_id")) for item in items]
    request_id = request_id or str(uuid.uuid4())
    attempt_id = str(uuid.uuid4())
    if db.get(RecommendationRequest, request_id) is None:
        parent_request_id = fact.get("parent_request_id")
        parent_request = (
            db.get(RecommendationRequest, parent_request_id)
            if parent_request_id else None
        )
        is_show_more = (
            fact.get("request_kind") == "show_more"
            and parent_request is not None
        )
        served_attempt_id = (
            parent_request.served_attempt_id
            if is_show_more and parent_request else attempt_id
        )
        db.add(RecommendationRequest(
            request_id=request_id,
            source_inbound_msg_id=source_inbound_msg_id or str(inbound_event_id),
            request_index=int(fact.get("request_index", reply_index)),
            request_kind=str(fact.get("request_kind") or "initial_search"),
            parent_request_id=parent_request_id,
            served_attempt_id=served_attempt_id,
            snapshot_id=snapshot_id,
            viewer_userid=userid,
            direction=str(ctx.get("direction") or "search_job"),
            query_digest=str(ctx.get("query_digest") or "")[:16],
            execution_mode="on",
            served_assignment=str(ctx.get("assignment") or "candidate"),
            served_strategy_version_id=ctx.get("strategy_version_id"),
            algorithm_version=str(ctx.get("algorithm_version") or "recommendation-v1"),
            final_candidate_count=int(fact.get("candidate_count", len(candidate_ids))),
            result_count=len(candidate_ids),
            is_zero_result=not candidate_ids,
            served_top_ids=list(fact.get("served_top_ids") or candidate_ids),
        ))
        db.flush()
        if not is_show_more:
            db.add(RecommendationSearchAttempt(
                attempt_id=attempt_id,
                request_id=request_id,
                attempt_no=1,
                attempt_kind=str(fact.get("request_kind") or "initial_search"),
                criteria_digest=str(ctx.get("query_digest") or ""),
                scoring_time_utc=datetime.now(timezone.utc),
                candidate_count=int(fact.get("candidate_count", len(candidate_ids))),
                candidate_ids=list(fact.get("candidate_ids") or candidate_ids),
                precision_pool_ids=list(fact.get("precision_pool_ids") or candidate_ids),
                result_count=len(candidate_ids),
                is_zero_result=not candidate_ids,
                strategy_version_id=ctx.get("strategy_version_id"),
                algorithm_version=str(ctx.get("algorithm_version") or "recommendation-v1"),
                llm_status="completed",
            ))
    resolved_delivery_id = delivery_id or str(uuid.uuid4())
    delivery = RecommendationDelivery(
        delivery_id=resolved_delivery_id,
        source_inbound_msg_id=source_inbound_msg_id or str(inbound_event_id),
        reply_index=reply_index,
        request_id=request_id,
        snapshot_id=snapshot_id,
        userid=userid,
        content_ciphertext=encrypt_body(body).encode("ascii"),
        content_expires_at=datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes),
        recommendation_context=ctx or {"position_count": position_count, "items": []},
        session_commit_token=resolved_delivery_id,
        next_attempt_at=datetime.now(timezone.utc),
        status="prepared",
    )
    db.add(delivery)
    db.flush()
    db.add(WecomOutboundOutbox(
        inbound_event_id=inbound_event_id,
        reply_index=reply_index,
        userid=userid,
        msg_type="text",
        content=None,
        recommendation_delivery_id=delivery.delivery_id,
        status="pending",
    ))
    return delivery


def mark_delivery_sent(db: Session, delivery_id: str, provider_msg_id: str | None = None) -> None:
    delivery = db.get(RecommendationDelivery, delivery_id)
    if delivery:
        delivery.status = "sent"
        delivery.wecom_msgid = provider_msg_id
        delivery.sent_at = datetime.now(timezone.utc)
        delivery.lease_owner = None
        delivery.lease_expires_at = None


def redact_delivery(db: Session, delivery_id: str) -> bool:
    delivery = db.get(RecommendationDelivery, delivery_id)
    if not delivery or not delivery.content_ciphertext:
        return False
    delivery.content_ciphertext = None
    delivery.status = "redacted"
    return True
