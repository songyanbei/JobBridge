"""Durable media ownership and fail-closed hard-delete guard."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models import MediaAssetLifecycle
from app.services import demo_scope
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
            demo_id=demo_scope.demo_id_or_none(),
            object_key=key,
            owner_userid=owner_userid,
            operation_id=operation_id,
            state="pending",
            draft_expires_at=datetime.utcnow() + timedelta(minutes=draft_ttl_minutes),
        )
        db.add(row)
        db.flush()
        demo_scope.register(db, "media_asset_lifecycle", row.id)
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
    entity_version: int | None = None,
    max_media: int = 5,
) -> list[str]:
    unique_ids = sorted(set(media_ids))
    if len(unique_ids) > max(1, int(max_media)):
        raise ValueError("media_count_exceeds_limit")
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
        # A pending row may be replayed only for the same operation.  Once it
        # is attached, the state guard above prevents a second binding.
        row.state = "attached"
        row.entity_type = entity_type
        row.entity_id = entity_id
        row.entity_version = int(entity_version) if entity_version is not None else None
        row.draft_expires_at = None
    return [row.object_key for row in rows]


def attached_media_keys(
    db: Session,
    entity_type: str,
    entity_id: int,
    *,
    entity_version: int | None = None,
) -> list[str]:
    """Return only attached media belonging to the requested entity version."""
    if entity_type not in ("job", "resume"):
        raise ValueError("unsupported_media_entity_type")
    query = db.query(MediaAssetLifecycle).filter(
        MediaAssetLifecycle.entity_type == entity_type,
        MediaAssetLifecycle.entity_id == entity_id,
        MediaAssetLifecycle.state == "attached",
    )
    if entity_version is not None:
        query = query.filter(MediaAssetLifecycle.entity_version == int(entity_version))
    rows = query.order_by(MediaAssetLifecycle.id).all()
    return [row.object_key for row in rows]


def bind_job_media_version(
    db: Session,
    media_ids: list[int],
    job_id: int,
    *,
    owner_userid: str,
    job_version: int,
) -> list[str]:
    """Bind a bounded media set to a concrete Job version."""
    return attach_media(
        db,
        media_ids,
        "job",
        job_id,
        owner_userid=owner_userid,
        entity_version=job_version,
        max_media=5,
    )


def mark_delete_pending(db: Session, media_ids: list[int]) -> None:
    unique_ids = sorted(set(media_ids))
    if not unique_ids:
        return
    rows = (
        db.query(MediaAssetLifecycle)
        .populate_existing()
        .filter(
            MediaAssetLifecycle.id.in_(unique_ids),
            MediaAssetLifecycle.state.in_(("pending", "attached")),
        )
        .order_by(MediaAssetLifecycle.id)
        .with_for_update()
        .all()
    )
    now = datetime.utcnow()
    for row in rows:
        row.state = "delete_pending"
        row.next_attempt_at = now


def discard_pending_media(db: Session, media_id: int) -> bool:
    """只回收尚未绑定实体的草稿媒体，重放不得删除 attached 媒体。"""
    row = (
        db.query(MediaAssetLifecycle)
        .populate_existing()
        .filter(
            MediaAssetLifecycle.id == media_id,
            MediaAssetLifecycle.state == "pending",
            MediaAssetLifecycle.entity_type.is_(None),
            MediaAssetLifecycle.entity_id.is_(None),
        )
        .with_for_update()
        .one_or_none()
    )
    if row is None:
        return False
    row.state = "delete_pending"
    row.next_attempt_at = datetime.utcnow()
    return True


def mark_entity_media_delete_pending(
    db: Session,
    entity_type: str,
    entity_id: int,
    *,
    include_pending: bool = False,
) -> int:
    if entity_type not in ("job", "resume"):
        raise ValueError("unsupported_media_entity_type")
    states = ("pending", "attached") if include_pending else ("attached",)
    rows = (
        db.query(MediaAssetLifecycle)
        .populate_existing()
        .filter(
            MediaAssetLifecycle.entity_type == entity_type,
            MediaAssetLifecycle.entity_id == entity_id,
            MediaAssetLifecycle.state.in_(states),
        )
        .order_by(MediaAssetLifecycle.id)
        .with_for_update()
        .all()
    )
    now = datetime.utcnow()
    for row in rows:
        row.state = "delete_pending"
        row.next_attempt_at = now
    return len(rows)


def mark_job_media_delete_pending(
    db: Session, job_id: int, *, include_pending: bool = False,
) -> int:
    return mark_entity_media_delete_pending(
        db, "job", job_id, include_pending=include_pending,
    )


def mark_resume_media_delete_pending(
    db: Session, resume_id: int, *, include_pending: bool = False,
) -> int:
    return mark_entity_media_delete_pending(
        db, "resume", resume_id, include_pending=include_pending,
    )


def entity_hard_delete_media_complete(
    db: Session,
    entity_type: str,
    entity_id: int,
    images,
) -> bool:
    if entity_type not in ("job", "resume"):
        return False
    try:
        values = json.loads(images) if isinstance(images, str) else (images or [])
        if not isinstance(values, list):
            return False
        keys = {normalize_storage_reference(v) for v in values}
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    all_rows = db.query(MediaAssetLifecycle).filter(
        MediaAssetLifecycle.entity_type == entity_type,
        MediaAssetLifecycle.entity_id == entity_id,
    ).all()
    if not keys:
        return all(row.state == "deleted" for row in all_rows)
    by_key = {row.object_key: row for row in all_rows}
    return (
        keys.issubset(by_key)
        and all(by_key[key].state == "deleted" for key in keys)
        and all(row.state == "deleted" for row in all_rows)
    )


def hard_delete_media_complete(db: Session, job_id: int, images) -> bool:
    return entity_hard_delete_media_complete(db, "job", job_id, images)


def resume_hard_delete_media_complete(db: Session, resume_id: int, images) -> bool:
    return entity_hard_delete_media_complete(db, "resume", resume_id, images)
