"""Lease and delete media objects without losing durable retry state."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from app.db import SessionLocal
from app.models import MediaAssetLifecycle
from app.storage import get_storage
from app.tasks.common import log_event, task_lock

logger = logging.getLogger(__name__)

MEDIA_DELETE_MAX_ATTEMPTS = 10
MEDIA_DELETE_LEASE = timedelta(minutes=2)


def _claim_ids(db, owner: str, now: datetime, limit: int) -> list[int]:
    db.query(MediaAssetLifecycle).filter(
        MediaAssetLifecycle.state == "pending",
        MediaAssetLifecycle.draft_expires_at.isnot(None),
        MediaAssetLifecycle.draft_expires_at <= now,
    ).update(
        {"state": "delete_pending", "next_attempt_at": now},
        synchronize_session=False,
    )
    rows = (
        db.query(MediaAssetLifecycle)
        .filter(
            MediaAssetLifecycle.state == "delete_pending",
            (MediaAssetLifecycle.next_attempt_at.is_(None))
            | (MediaAssetLifecycle.next_attempt_at <= now),
            (MediaAssetLifecycle.lease_expires_at.is_(None))
            | (MediaAssetLifecycle.lease_expires_at <= now),
        )
        .order_by(MediaAssetLifecycle.id)
        .with_for_update(skip_locked=True)
        .limit(limit)
        .all()
    )
    for row in rows:
        row.lease_owner = owner
        row.lease_expires_at = now + MEDIA_DELETE_LEASE
    db.commit()
    return [row.id for row in rows]


def _renew_claimed_object_key(
    db,
    media_id: int,
    owner: str,
    now: datetime,
) -> str | None:
    """Fence a claimed row and renew its lease immediately before I/O."""
    row = db.query(MediaAssetLifecycle).filter(
        MediaAssetLifecycle.id == media_id,
        MediaAssetLifecycle.state == "delete_pending",
        MediaAssetLifecycle.lease_owner == owner,
        MediaAssetLifecycle.lease_expires_at > now,
    ).with_for_update().first()
    if row is None:
        return None
    row.lease_expires_at = now + MEDIA_DELETE_LEASE
    object_key = row.object_key
    db.commit()
    return object_key


def _apply_delete_result(
    row: MediaAssetLifecycle,
    *,
    error: Exception | None,
    now: datetime,
) -> str:
    """Apply one storage result without losing the durable object reference."""
    if error is None:
        row.state = "deleted"
        row.deleted_at = now
        row.last_error = None
        row.next_attempt_at = None
        outcome = "deleted"
    else:
        attempts = int(row.attempt_count or 0) + 1
        row.attempt_count = attempts
        row.last_error = str(error)[:255]
        if attempts >= MEDIA_DELETE_MAX_ATTEMPTS:
            row.state = "dead_letter"
            row.next_attempt_at = None
            outcome = "dead_letter"
            log_event(
                "media_cleanup_dead_lettered",
                media_id=int(row.id),
                entity_type=row.entity_type,
                entity_id=int(row.entity_id) if row.entity_id is not None else None,
                attempt_count=attempts,
                severity="alert",
            )
        else:
            row.state = "delete_pending"
            row.next_attempt_at = now + timedelta(
                seconds=min(3600, 2 ** min(attempts, 10))
            )
            outcome = "retry_wait"
    row.lease_owner = None
    row.lease_expires_at = None
    return outcome


def _finish_claimed_result(
    db,
    media_id: int,
    owner: str,
    *,
    error: Exception | None,
    now: datetime,
) -> str | None:
    """Persist a result only while this owner still holds an unexpired lease."""
    row = db.query(MediaAssetLifecycle).filter(
        MediaAssetLifecycle.id == media_id,
        MediaAssetLifecycle.state == "delete_pending",
        MediaAssetLifecycle.lease_owner == owner,
        MediaAssetLifecycle.lease_expires_at > now,
    ).with_for_update().first()
    if row is None:
        return None
    outcome = _apply_delete_result(row, error=error, now=now)
    db.commit()
    return outcome


def run(limit: int = 100) -> None:
    owner = f"media-{uuid.uuid4()}"
    with task_lock("media_cleanup_worker", ttl=300) as acquired:
        if not acquired:
            return
        with SessionLocal() as db:
            ids = _claim_ids(db, owner, datetime.utcnow(), limit)

        storage = None
        storage_error = None
        try:
            storage = get_storage()
        except Exception as exc:
            storage_error = exc
        deleted = 0
        dead_lettered = 0
        for media_id in ids:
            with SessionLocal() as db:
                object_key = _renew_claimed_object_key(
                    db,
                    media_id,
                    owner,
                    datetime.utcnow(),
                )
                if object_key is None:
                    continue
            error = storage_error
            if error is None:
                try:
                    confirmed = storage.delete(object_key)
                    if confirmed is not True:
                        error = RuntimeError("storage delete was not confirmed")
                except Exception as exc:
                    error = exc

            with SessionLocal() as db:
                outcome = _finish_claimed_result(
                    db,
                    media_id,
                    owner,
                    error=error,
                    now=datetime.utcnow(),
                )
                if outcome is None:
                    continue
                deleted += int(outcome == "deleted")
                dead_lettered += int(outcome == "dead_letter")
        logger.info(
            "media_cleanup_completed scanned=%s deleted=%s dead_lettered=%s",
            len(ids),
            deleted,
            dead_lettered,
        )
