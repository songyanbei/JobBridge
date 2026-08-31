"""Atomic domain-outbox helpers for Job/Resume fact source changes."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
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


def claim_domain_events(db, *, owner: str, limit: int = 100, lease_seconds: int = 60, now: datetime | None = None) -> list[DomainOutboxEvent]:
    """Claim pending/retryable events with a fenced lease, without committing."""
    if not owner or lease_seconds <= 0:
        raise ValueError("owner and positive lease_seconds are required")
    now = (now or datetime.now(timezone.utc)).replace(tzinfo=None)
    rows = db.query(DomainOutboxEvent).filter(
        ((DomainOutboxEvent.status == "pending") & ((DomainOutboxEvent.next_attempt_at.is_(None)) | (DomainOutboxEvent.next_attempt_at <= now)))
        | ((DomainOutboxEvent.status == "processing") & (DomainOutboxEvent.lease_until < now)),
    ).order_by(DomainOutboxEvent.occurred_at, DomainOutboxEvent.id).with_for_update(skip_locked=True).limit(int(limit)).all()
    lease_until = now + timedelta(seconds=int(lease_seconds))
    for row in rows:
        row.status = "processing"
        row.lease_owner = owner
        row.lease_until = lease_until
        row.fencing_token = int(row.fencing_token or 0) + 1
    db.flush()
    return rows


def finalize_domain_event(
    db, event_id: int, *, owner: str, fencing_token: int, success: bool = True,
    error: str | None = None, max_attempts: int = 5, retry_seconds: int = 30,
    now: datetime | None = None,
) -> bool:
    """Finalize a claimed event only while its lease/fence is valid."""
    now = (now or datetime.now(timezone.utc)).replace(tzinfo=None)
    row = db.query(DomainOutboxEvent).filter(
        DomainOutboxEvent.id == int(event_id), DomainOutboxEvent.status == "processing",
        DomainOutboxEvent.lease_owner == owner, DomainOutboxEvent.fencing_token == int(fencing_token),
        DomainOutboxEvent.lease_until > now,
    ).with_for_update().first()
    if row is None:
        return False
    row.lease_owner = None
    row.lease_until = None
    if success:
        row.status = "published"
        row.last_error = None
    else:
        row.attempt_count = int(row.attempt_count or 0) + 1
        row.last_error = (error or "consumer_error")[:255]
        if row.attempt_count >= int(max_attempts):
            row.status = "dead_letter"
        else:
            row.status = "pending"
            row.next_attempt_at = now + timedelta(seconds=max(1, int(retry_seconds)))
    db.flush()
    return True


def job_event_is_current(db, event: DomainOutboxEvent) -> bool:
    """Reload the matching fact source and reject stale/late events.

    Unknown aggregate types fail closed. Tombstones are valid only when the
    current row is itself deleted/delisted and at the exact aggregate version.
    """
    aggregate_type = str(event.aggregate_type or "").lower()
    if aggregate_type == "job":
        from app.models import Job
        model = Job
    elif aggregate_type == "resume":
        from app.models import Resume
        model = Resume
    else:
        return False
    row = db.query(model).filter(model.id == int(event.aggregate_id)).populate_existing().first()
    if row is None:
        return bool(event.tombstone)
    current_version = int(getattr(row, "aggregate_version", None) or row.version or 1)
    if current_version != int(event.aggregate_version):
        return False
    if event.tombstone:
        return bool(row.deleted_at is not None or row.delist_reason is not None)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return bool(row.audit_status == "passed" and row.deleted_at is None and row.delist_reason is None and row.expires_at and row.expires_at > now)


def consume_pending_events(db, handler, *, owner: str, limit: int = 100, lease_seconds: int = 60, max_attempts: int = 5) -> dict[str, int]:
    """Claim, handle, and fence-finalize one batch; handler receives each event."""
    claimed = claim_domain_events(db, owner=owner, limit=limit, lease_seconds=lease_seconds)
    stats = {"claimed": len(claimed), "published": 0, "retryable": 0, "dead_letter": 0, "stale": 0}
    for event in claimed:
        try:
            if not job_event_is_current(db, event):
                stats["stale"] += 1
                finalize_domain_event(db, event.id, owner=owner, fencing_token=event.fencing_token, success=True)
                continue
            handler(event)
            if finalize_domain_event(db, event.id, owner=owner, fencing_token=event.fencing_token, success=True):
                stats["published"] += 1
        except Exception as exc:  # noqa: BLE001
            if finalize_domain_event(db, event.id, owner=owner, fencing_token=event.fencing_token, success=False, error=str(exc), max_attempts=max_attempts):
                stats["dead_letter" if int(event.attempt_count or 0) >= max_attempts else "retryable"] += 1
    db.commit()
    return stats


__all__ = ["append_domain_event", "claim_domain_events", "consume_pending_events", "event_is_current", "finalize_domain_event", "job_event_is_current", "payload_digest"]
