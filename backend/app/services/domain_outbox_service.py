"""Atomic domain-outbox helpers for Job/Resume fact source changes."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.models import DomainOutboxEvent

_PII_KEYS = {"phone", "contact", "contact_person", "wechat", "wechat_id", "mobile"}


def _safe_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _safe_payload(v) for k, v in value.items() if str(k).lower() not in _PII_KEYS}
    if isinstance(value, list):
        return [_safe_payload(item) for item in value]
    return value


def payload_digest(payload: dict[str, Any]) -> str:
    body = json.dumps(_safe_payload(payload), ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def append_domain_event(
    db,
    *,
    aggregate_type: str,
    aggregate_id: int,
    aggregate_version: int,
    event_type: str,
    payload: dict[str, Any] | None = None,
    trace_id: str | None = None,
    tombstone: bool = False,
    occurred_at: datetime | None = None,
) -> DomainOutboxEvent:
    """Insert one versioned event in the caller's transaction (never commit)."""
    if not aggregate_type or aggregate_id is None or int(aggregate_version) < 1 or not event_type:
        raise ValueError("invalid_domain_event_identity")
    safe = _safe_payload(payload or {})
    existing = db.query(DomainOutboxEvent).filter_by(
        aggregate_type=str(aggregate_type), aggregate_id=int(aggregate_id),
        aggregate_version=int(aggregate_version), event_type=str(event_type),
    ).first()
    if existing is not None:
        return existing
    event = DomainOutboxEvent(
        aggregate_type=str(aggregate_type), aggregate_id=int(aggregate_id),
        aggregate_version=int(aggregate_version), event_type=str(event_type),
        payload=safe, payload_digest=payload_digest(safe), trace_id=trace_id,
        tombstone=1 if tombstone else 0,
        occurred_at=(occurred_at or datetime.now(timezone.utc)).replace(tzinfo=None),
    )
    try:
        # Isolate the uniqueness race in a savepoint.  Rolling back the whole
        # Session here would discard the caller's business mutation and break
        # the required fact+event transaction boundary.
        with db.begin_nested():
            db.add(event)
            db.flush()
    except IntegrityError:
        # Duplicate delivery is idempotent; leave the existing row untouched.
        existing = db.query(DomainOutboxEvent).filter_by(
            aggregate_type=str(aggregate_type), aggregate_id=int(aggregate_id),
            aggregate_version=int(aggregate_version), event_type=str(event_type),
        ).first()
        if existing is None:
            raise
        return existing
    return event


def event_is_current(event: DomainOutboxEvent, *, current_version: int, active: bool) -> bool:
    """Consumer guard: reject stale versions and inactive/tombstoned projections."""
    if int(event.aggregate_version) < int(current_version):
        return False
    if int(event.aggregate_version) > int(current_version):
        return False
    return bool(active) and not bool(event.tombstone)


__all__ = ["append_domain_event", "event_is_current", "payload_digest"]
