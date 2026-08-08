"""Lease and delete media objects without losing durable retry state."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from app.db import SessionLocal
from app.models import MediaAssetLifecycle
from app.storage import get_storage
from app.tasks.common import task_lock

logger = logging.getLogger(__name__)


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
        row.lease_expires_at = now + timedelta(minutes=2)
    db.commit()
    return [row.id for row in rows]


def run(limit: int = 100) -> None:
    owner = f"media-{uuid.uuid4()}"
    with task_lock("media_cleanup_worker", ttl=300) as acquired:
        if not acquired:
            return
        with SessionLocal() as db:
            ids = _claim_ids(db, owner, datetime.utcnow(), limit)

        storage = get_storage()
        deleted = 0
        for media_id in ids:
            with SessionLocal() as db:
                row = db.query(MediaAssetLifecycle).filter(
                    MediaAssetLifecycle.id == media_id,
                    MediaAssetLifecycle.lease_owner == owner,
                ).first()
                if row is None:
                    continue
                object_key = row.object_key
            error = None
            try:
                storage.delete(object_key)
            except Exception as exc:
                error = exc

            with SessionLocal() as db:
                row = db.query(MediaAssetLifecycle).filter(
                    MediaAssetLifecycle.id == media_id,
                    MediaAssetLifecycle.lease_owner == owner,
                ).with_for_update().first()
                if row is None:
                    continue
                try:
                    if error is None:
                        row.state = "deleted"
                        row.deleted_at = datetime.utcnow()
                        row.last_error = None
                        row.next_attempt_at = None
                        deleted += 1
                    else:
                        raise error
                except Exception as exc:
                    row.attempt_count = int(row.attempt_count or 0) + 1
                    row.last_error = str(exc)[:255]
                    row.next_attempt_at = datetime.utcnow() + timedelta(
                        seconds=min(3600, 2 ** min(row.attempt_count, 10))
                    )
                finally:
                    row.lease_owner = None
                    row.lease_expires_at = None
                db.commit()
        logger.info("media_cleanup_completed scanned=%s deleted=%s", len(ids), deleted)
