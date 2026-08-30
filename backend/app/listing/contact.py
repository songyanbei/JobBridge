"""Contact domain boundary (B0).

The service owns opaque request/grant identifiers and is the only boundary at
which a future B1 encrypted contact value may be read.  B0 deliberately keeps
the feature fail-closed: when ``contact_service_mode`` is ``off`` no grant is
issued or redeemed and no contact value is returned.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.models import ContactAccessAudit, ContactDelivery, ContactGrant, ContactRequest, WecomOutboundOutbox
from app.schemas.contact import (
    ContactGrantMetadata,
    ContactRequestCreate,
    ContactRequestView,
    ContactResponse,
)

CONTACT_UNAVAILABLE_MESSAGE = "暂时无法提供联系方式，请稍后重试。"
CONTACT_PLATFORM_REQUEST_MESSAGE = "联系请求已提交，请通过平台联系对方。"


class ContactDeliveryError(RuntimeError):
    """Delivery cannot be sent without a valid encrypted payload."""


@dataclass(frozen=True)
class ContactDeliveryHandle:
    """Short-lived in-process plaintext handle; never serialized or persisted."""

    delivery_id: str
    channel: str
    payload: str


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _digest(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def create_contact_request_id() -> str:
    """Generate a high-entropy opaque request reference (never reversible)."""
    return "cr_" + secrets.token_urlsafe(24).rstrip("=")


def _new_grant_id() -> str:
    return "cg_" + secrets.token_urlsafe(24).rstrip("=")


@dataclass(frozen=True)
class ContactDecision:
    allowed: bool
    reason_code: str
    request_id: str | None = None


class ContactService:
    """Server-side contact request/grant state machine."""

    def __init__(self, db: Session | None = None, *, mode: str | None = None, redis_client=None, rate_window_seconds: int | None = None, rate_limit: int | None = None, daily_limit: int | None = None):
        self.db = db
        self.mode = str(mode if mode is not None else getattr(settings, "contact_service_mode", "off")).lower()
        self.redis_client = redis_client
        self.rate_window_seconds = max(1, int(rate_window_seconds if rate_window_seconds is not None else getattr(settings, "contact_rate_per_listing_window_seconds", 600)))
        self.rate_limit = max(1, int(rate_limit if rate_limit is not None else getattr(settings, "contact_rate_per_listing_limit", 3)))
        self.daily_limit = max(1, int(daily_limit if daily_limit is not None else getattr(settings, "contact_daily_limit", 30)))

    @property
    def enabled(self) -> bool:
        return self.mode in {"shadow", "on"}

    @staticmethod
    def unavailable() -> ContactResponse:
        return ContactResponse(success=False, code="contact_unavailable", message=CONTACT_UNAVAILABLE_MESSAGE)

    def create_contact_request(
        self,
        actor_id: str,
        listing_ref: str,
        *,
        action: str = "request_contact",
        listing_version: int | None = None,
        policy_version: str | None = None,
        trace_id: str | None = None,
        expires_in_seconds: int | None = None,
        db: Session | None = None,
    ) -> ContactRequestView:
        """Create an opaque request; this operation never reads contact PII."""
        actor_id, listing_ref = str(actor_id).strip(), str(listing_ref).strip()
        if not actor_id or not listing_ref:
            raise ValueError("actor_id and listing_ref are required")
        now = _now()
        ttl = max(1, int(expires_in_seconds or getattr(settings, "contact_grant_ttl_seconds", 60)))
        request_id = create_contact_request_id()
        nonce = secrets.token_urlsafe(32)
        request = ContactRequest(
            request_id=request_id,
            actor_id=actor_id,
            listing_ref=listing_ref,
            action=action,
            request_digest=_digest({"actor": actor_id, "listing": listing_ref, "action": action}),
            nonce_digest=_digest(nonce),
            listing_version=listing_version,
            policy_version=policy_version,
            status="pending",
            expires_at=now + timedelta(seconds=ttl),
            trace_id=trace_id,
        )
        session = db or self.db
        if session is not None:
            session.add(request)
            session.flush()
        return ContactRequestView(
            request_id=request.request_id,
            listing_ref=request.listing_ref,
            action="request_contact",
            status="pending",
            expires_at=request.expires_at,
            trace_id=request.trace_id,
        )

    def authorize_contact(
        self,
        request_id: str,
        actor_id: str,
        listing_ref: str,
        *,
        listing_version: int | None = None,
        policy_version: str | None = None,
        db: Session | None = None,
    ) -> ContactDecision:
        """Re-check actor/listing/version/policy on the server before issuing."""
        session = db or self.db
        if not self.enabled:
            return ContactDecision(False, "contact_service_off", request_id)
        if session is None:
            return ContactDecision(False, "db_unavailable", request_id)
        request = session.query(ContactRequest).filter(ContactRequest.request_id == str(request_id)).with_for_update().first()
        now = _now()
        if request is None:
            return ContactDecision(False, "invalid_request", request_id)
        if request.actor_id != str(actor_id) or request.listing_ref != str(listing_ref):
            self.audit_contact_event("authorize", "denied", "actor_or_listing_mismatch", actor_id=actor_id, listing_ref=listing_ref, request_id=request_id, db=session)
            return ContactDecision(False, "forbidden", request_id)
        if request.expires_at <= now:
            request.status = "expired"
            self.audit_contact_event("authorize", "denied", "request_expired", actor_id=actor_id, listing_ref=listing_ref, request_id=request_id, db=session)
            return ContactDecision(False, "expired", request_id)
        if request.status in {"revoked", "expired"}:
            return ContactDecision(False, request.status, request_id)
        if listing_version is not None and request.listing_version not in (None, int(listing_version)):
            return ContactDecision(False, "stale_listing", request_id)
        if policy_version is not None and request.policy_version not in (None, str(policy_version)):
            return ContactDecision(False, "stale_policy", request_id)
        request.status = "authorized"
        self.audit_contact_event("authorize", "allowed", "authorized", actor_id=actor_id, listing_ref=listing_ref, request_id=request_id, db=session)
        return ContactDecision(True, "authorized", request_id)

    def issue_one_time_grant(
        self,
        request_id: str,
        actor_id: str,
        listing_ref: str,
        *,
        listing_version: int | None = None,
        policy_version: str | None = None,
        trace_id: str | None = None,
        db: Session | None = None,
    ) -> ContactGrantMetadata | ContactResponse:
        if not self.enabled:
            return self.unavailable()
        session = db or self.db
        if session is None:
            return ContactResponse(success=False, code="contact_unavailable", message=CONTACT_UNAVAILABLE_MESSAGE)
        decision = self.authorize_contact(request_id, actor_id, listing_ref, listing_version=listing_version, policy_version=policy_version, db=session)
        if not decision.allowed:
            code = "forbidden" if decision.reason_code in {"forbidden", "invalid_request"} else "expired"
            return ContactResponse(success=False, code=code, message=CONTACT_UNAVAILABLE_MESSAGE)
        token = secrets.token_urlsafe(32)
        grant = ContactGrant(
            grant_id=_new_grant_id(), request_id=str(request_id), actor_id=str(actor_id), listing_ref=str(listing_ref),
            action="request_contact", token_hash=_digest(token), nonce_digest=_digest(secrets.token_bytes(32)),
            listing_version=listing_version, policy_version=policy_version, status="issued",
            expires_at=_now() + timedelta(seconds=max(1, int(getattr(settings, "contact_grant_ttl_seconds", 60)))), trace_id=trace_id,
        )
        session.add(grant)
        self.audit_contact_event("grant_issue", "allowed", "issued", actor_id=actor_id, listing_ref=listing_ref, request_id=request_id, grant_id=grant.grant_id, trace_id=trace_id, db=session)
        session.flush()
        return ContactGrantMetadata(grant_id=grant.grant_id, token=token, expires_at=grant.expires_at)

    def redeem_grant(self, grant_id: str, token: str, actor_id: str, *, db: Session | None = None, trace_id: str | None = None, inbound_event_id: int | None = None, reply_index: int = 0, userid: str | None = None, current_listing_version: int | None = None, current_policy_version: str | None = None, listing_status: str | None = None, actor_status: str | None = None) -> ContactResponse:
        """Consume a grant once and create one stable delivery reference."""
        if not self.enabled:
            return self.unavailable()
        session = db or self.db
        if session is None:
            return self.unavailable()
        if not self._rate_allowed(actor_id, grant_id, session):
            self.audit_contact_event("grant_redeem", "denied", "rate_limited", actor_id=actor_id, grant_id=grant_id, trace_id=trace_id, db=session)
            return ContactResponse(success=False, code="rate_limited", message=CONTACT_UNAVAILABLE_MESSAGE)
        grant = session.query(ContactGrant).filter(ContactGrant.grant_id == str(grant_id)).with_for_update().first()
        if grant is None or not secrets.compare_digest(str(grant.token_hash), _digest(token)):
            self.audit_contact_event("grant_redeem", "denied", "invalid_grant", actor_id=actor_id, grant_id=grant_id, trace_id=trace_id, db=session)
            return ContactResponse(success=False, code="invalid_grant", message=CONTACT_UNAVAILABLE_MESSAGE)
        if grant.actor_id != str(actor_id):
            self.audit_contact_event("grant_redeem", "denied", "actor_mismatch", actor_id=actor_id, grant_id=grant_id, trace_id=trace_id, db=session)
            return ContactResponse(success=False, code="forbidden", message=CONTACT_UNAVAILABLE_MESSAGE)
        # Re-authorize mutable facts while the grant row is locked. Callers
        # must provide these facts from a fresh listing/actor lookup; omitted
        # values preserve B0 compatibility but never permit a mismatch.
        if current_listing_version is not None and grant.listing_version is not None and int(current_listing_version) != int(grant.listing_version):
            self.audit_contact_event("grant_redeem", "denied", "listing_version_changed", actor_id=actor_id, listing_ref=grant.listing_ref, grant_id=grant_id, trace_id=trace_id, db=session)
            return ContactResponse(success=False, code="forbidden", message=CONTACT_UNAVAILABLE_MESSAGE)
        if current_policy_version is not None and grant.policy_version is not None and str(current_policy_version) != str(grant.policy_version):
            self.audit_contact_event("grant_redeem", "denied", "policy_version_changed", actor_id=actor_id, listing_ref=grant.listing_ref, grant_id=grant_id, trace_id=trace_id, db=session)
            return ContactResponse(success=False, code="forbidden", message=CONTACT_UNAVAILABLE_MESSAGE)
        if listing_status is not None and str(listing_status).lower() not in {"active", "passed", "published"}:
            self.audit_contact_event("grant_redeem", "denied", "listing_not_active", actor_id=actor_id, listing_ref=grant.listing_ref, grant_id=grant_id, trace_id=trace_id, db=session)
            return ContactResponse(success=False, code="forbidden", message=CONTACT_UNAVAILABLE_MESSAGE)
        if actor_status is not None and str(actor_status).lower() in {"blocked", "deleted", "disabled"}:
            self.audit_contact_event("grant_redeem", "denied", "actor_not_allowed", actor_id=actor_id, listing_ref=grant.listing_ref, grant_id=grant_id, trace_id=trace_id, db=session)
            return ContactResponse(success=False, code="forbidden", message=CONTACT_UNAVAILABLE_MESSAGE)
        if grant.expires_at <= _now():
            grant.status = "expired"
            return ContactResponse(success=False, code="expired", message=CONTACT_UNAVAILABLE_MESSAGE)
        if grant.status == "used":
            return ContactResponse(success=False, code="already_used", message=CONTACT_UNAVAILABLE_MESSAGE)
        if grant.status == "revoked":
            return ContactResponse(success=False, code="revoked", message=CONTACT_UNAVAILABLE_MESSAGE)
        # ``platform_request`` is a safe, PII-free acknowledgement.  It uses
        # a fixed Worker template and therefore does not need a ciphertext;
        # channels carrying actual contact values must provide ciphertext; the
        # dispatcher never falls back to legacy phone or WeChat columns.
        now = _now()
        grant.status, grant.used_at = "used", now
        delivery = ContactDelivery(
            delivery_id="cd_" + secrets.token_urlsafe(24).rstrip("="), grant_id=grant.grant_id,
            actor_id=grant.actor_id, listing_ref=grant.listing_ref, channel="platform_request",
            content_ciphertext=None, key_version=None, content_hash=None,
            status="prepared", expires_at=_now() + timedelta(seconds=max(1, int(getattr(settings, "contact_delivery_ttl_seconds", 300)))),
        )
        session.add(delivery)
        if inbound_event_id is not None:
            # Same transaction: the outbox contains only an opaque delivery
            # reference and a template marker, never a token or PII value.
            session.add(WecomOutboundOutbox(
                inbound_event_id=int(inbound_event_id), reply_index=max(0, int(reply_index)),
                userid=str(userid or actor_id), msg_type="text", content=None,
                contact_delivery_id=delivery.delivery_id, intent="contact_request",
                status="pending",
            ))
        self.audit_contact_event("grant_redeem", "allowed", "redeemed", actor_id=actor_id, listing_ref=grant.listing_ref, grant_id=grant.grant_id, trace_id=trace_id, db=session)
        session.flush()
        return ContactResponse(success=True, code="ok", grant=None, message="联系请求已提交。")

    def _rate_allowed(self, actor_id: str, grant_id: str, session: Session) -> bool:
        """Redis is only a fast pre-check; DB audit remains authoritative."""
        if self.redis_client is not None:
            try:
                key = f"contact:rate:{_digest(actor_id)}"
                result = self.redis_client.incr(key)
                if int(result) == 1:
                    self.redis_client.expire(key, self.rate_window_seconds)
                if int(result) > self.rate_limit:
                    return False
            except Exception:
                # A broken limiter must never open the gate.
                return False
        cutoff = _now() - timedelta(seconds=self.rate_window_seconds)
        recent = session.query(ContactAccessAudit).filter(
            ContactAccessAudit.actor_hash == _digest(actor_id),
            ContactAccessAudit.event_type == "grant_redeem",
            ContactAccessAudit.outcome == "allowed",
            ContactAccessAudit.created_at >= cutoff,
        ).count()
        if recent >= self.rate_limit:
            return False
        day_cutoff = _now() - timedelta(days=1)
        daily = session.query(ContactAccessAudit).filter(
            ContactAccessAudit.actor_hash == _digest(actor_id),
            ContactAccessAudit.event_type == "grant_redeem",
            ContactAccessAudit.outcome == "allowed",
            ContactAccessAudit.created_at >= day_cutoff,
        ).count()
        return daily < self.daily_limit

    def load_delivery_for_send(self, delivery_id: str, *, crypto_service=None, db: Session | None = None) -> ContactDeliveryHandle:
        """Load/decrypt exactly once for a dispatcher; all failures fail closed."""
        session = db or self.db
        if session is None:
            raise ContactDeliveryError("db_unavailable")
        delivery = session.query(ContactDelivery).filter(ContactDelivery.delivery_id == str(delivery_id)).with_for_update().first()
        if delivery is None:
            raise ContactDeliveryError("delivery_not_found")
        if delivery.status in {"revoked", "expired", "sent"} or delivery.expires_at <= _now():
            if delivery.status not in {"revoked", "sent"}:
                delivery.status = "expired"
            raise ContactDeliveryError("delivery_not_sendable")
        if delivery.channel == "platform_request":
            return ContactDeliveryHandle(
                delivery_id=delivery.delivery_id,
                channel=delivery.channel,
                payload=CONTACT_PLATFORM_REQUEST_MESSAGE,
            )
        if not delivery.content_ciphertext:
            raise ContactDeliveryError("delivery_ciphertext_missing")
        if crypto_service is None:
            raise ContactDeliveryError("crypto_unavailable")
        try:
            payload = crypto_service.decrypt(
                delivery.content_ciphertext,
                field="delivery_payload",
                entity_type="contact_delivery",
                entity_id=delivery.delivery_id,
            )
        except Exception as exc:
            raise ContactDeliveryError("delivery_decrypt_failed") from exc
        return ContactDeliveryHandle(delivery_id=delivery.delivery_id, channel=delivery.channel, payload=payload)

    def dispatch_contact_delivery(self, delivery_id: str, sender, *, crypto_service=None, db: Session | None = None) -> bool:
        """Send one existing delivery; retries reuse this delivery id/payload."""
        session = db or self.db
        handle = self.load_delivery_for_send(delivery_id, crypto_service=crypto_service, db=session)
        delivery = session.query(ContactDelivery).filter(ContactDelivery.delivery_id == handle.delivery_id).with_for_update().one()
        outbox = session.query(WecomOutboundOutbox).filter(
            WecomOutboundOutbox.contact_delivery_id == handle.delivery_id,
        ).with_for_update().first()
        if outbox is not None:
            outbox.status = "sending"
        delivery.status = "sending"
        try:
            provider_result = sender(handle.payload, channel=handle.channel, delivery_id=handle.delivery_id)
        except Exception:
            delivery.status = "retry_wait"
            if outbox is not None:
                outbox.status = "pending"
                outbox.next_attempt_at = _now() + timedelta(seconds=5)
                outbox.last_error = "contact_delivery_send_failed"
            session.flush()
            raise
        delivery.status, delivery.sent_at = "sent", _now()
        if outbox is not None:
            outbox.status, outbox.sent_at = "sent", delivery.sent_at
            if isinstance(provider_result, dict):
                outbox.provider_msg_id = str(provider_result.get("msgid") or provider_result.get("message_id") or "")[:128] or None
            elif provider_result:
                outbox.provider_msg_id = str(provider_result)[:128]
        session.flush()
        return True

    def revoke_grant(self, grant_id: str, *, reason: str = "revoked", db: Session | None = None, trace_id: str | None = None) -> bool:
        session = db or self.db
        if session is None:
            return False
        grant = session.query(ContactGrant).filter(ContactGrant.grant_id == str(grant_id)).with_for_update().first()
        if grant is None:
            return False
        if grant.status not in {"used", "revoked"}:
            grant.status, grant.revoked_at, grant.revoke_reason = "revoked", _now(), str(reason)[:64]
        self.audit_contact_event("grant_revoke", "allowed", str(reason)[:64], actor_id=grant.actor_id, listing_ref=grant.listing_ref, grant_id=grant.grant_id, trace_id=trace_id, db=session)
        return True

    def audit_contact_event(self, event_type: str, outcome: str, reason_code: str, *, actor_id: str | None = None, listing_ref: str | None = None, request_id: str | None = None, grant_id: str | None = None, trace_id: str | None = None, db: Session | None = None) -> None:
        session = db or self.db
        if session is None:
            return
        session.add(ContactAccessAudit(
            event_id=str(uuid.uuid4()), event_type=str(event_type)[:32], outcome=str(outcome)[:32], reason_code=str(reason_code)[:64],
            actor_hash=_digest(actor_id) if actor_id else None, listing_hash=_digest(listing_ref) if listing_ref else None,
            request_id=request_id, grant_id=grant_id, trace_id=trace_id,
        ))


# Function aliases keep the domain boundary convenient for existing service style.
create_contact_request = ContactService.create_contact_request
authorize_contact = ContactService.authorize_contact
issue_one_time_grant = ContactService.issue_one_time_grant
redeem_grant = ContactService.redeem_grant
revoke_grant = ContactService.revoke_grant
audit_contact_event = ContactService.audit_contact_event
