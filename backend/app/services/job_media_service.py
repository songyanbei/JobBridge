"""Durable media ownership and fail-closed hard-delete guard."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models import MediaAssetLifecycle
from app.services.storage_reference_service import normalize_storage_reference


def record_pending_media(
    db: Session,
    object_key: str,
    *,
    owner_userid: str,
    operation_id: str | None = None,
    draft_ttl_minutes: int = 10,
) -> MediaAssetLifecycle:
    key = normalize_storage_reference(object_key)
    row = db.query(MediaAssetLifecycle).filter_by(object_key=key).first()
    if row is None:
        row = MediaAssetLifecycle(
            object_key=key,
            owner_userid=owner_userid,
            operation_id=operation_id,
            state="pending",
            draft_expires_at=datetime.utcnow() + timedelta(minutes=draft_ttl_minutes),
        )
        db.add(row)
        db.flush()
    elif row.owner_userid != owner_userid:
        raise ValueError("media_object_key_owned_by_another_user")
    return row


def attach_media(
    db: Session,
    media_ids: list[int],
    entity_type: str,
    entity_id: int,
    *,
    owner_userid: str | None = None,
) -> list[str]:
    unique_ids = sorted(set(media_ids))
    rows = (
        db.query(MediaAssetLifecycle)
        .filter(MediaAssetLifecycle.id.in_(unique_ids))
        .order_by(MediaAssetLifecycle.id)
        .with_for_update()
        .all()
    )
    if len(rows) != len(unique_ids) or any(row.state != "pending" for row in rows):
        raise ValueError("media_lifecycle_incomplete_or_consumed")
    if owner_userid is not None and any(row.owner_userid != owner_userid for row in rows):
        raise ValueError("media_lifecycle_owner_mismatch")
    for row in rows:
        row.state = "attached"
        row.entity_type = entity_type
        row.entity_id = entity_id
        row.draft_expires_at = None
    return [row.object_key for row in rows]


def mark_delete_pending(db: Session, media_ids: list[int]) -> None:
    if media_ids:
        db.query(MediaAssetLifecycle).filter(MediaAssetLifecycle.id.in_(media_ids)).update(
            {"state": "delete_pending", "next_attempt_at": datetime.utcnow()}, synchronize_session=False)


def hard_delete_media_complete(db: Session, job_id: int, images) -> bool:
    try:
        values = json.loads(images) if isinstance(images, str) else (images or [])
        if not isinstance(values, list):
            return False
        keys = {normalize_storage_reference(v) for v in values}
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not keys:
        return True
    rows = db.query(MediaAssetLifecycle).filter(
        MediaAssetLifecycle.entity_type == "job",
        MediaAssetLifecycle.entity_id == job_id,
        MediaAssetLifecycle.object_key.in_(keys),
    ).all()
    return len(rows) == len(keys) and all(row.state == "deleted" for row in rows)
