"""Exposure facts and rolling opportunity helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app.models import RecommendationDelivery, RecommendationImpression


def exposure_opportunities(counts: dict[str, int], candidate_ids: Iterable[str]) -> dict[str, float]:
    ids = [str(item) for item in candidate_ids]
    n = len(ids)
    if n <= 1:
        return {item: 0.5 for item in ids}
    values = {item: int(counts.get(item, 0) or 0) for item in ids}
    result: dict[str, float] = {}
    for item, count in values.items():
        lower = sum(value < count for value in values.values())
        equal = sum(value == count for value in values.values())
        percentile = (lower + 0.5 * (equal - 1)) / (n - 1)
        result[item] = 1 - percentile
    return result


def batch_candidate_exposures(
    db: Session,
    *,
    target_type: str,
    candidate_ids: Iterable[str | int],
    request_now_utc: datetime,
    window_hours: int = 168,
) -> dict[str, int]:
    ids = [int(item) for item in candidate_ids]
    if not ids:
        return {}
    now = request_now_utc.astimezone(timezone.utc)
    start = now - timedelta(hours=window_hours)
    rows = (
        db.query(
            RecommendationImpression.target_id,
            func.count(RecommendationImpression.id),
        )
        .filter(
            RecommendationImpression.target_type == target_type,
            RecommendationImpression.target_id.in_(ids),
            RecommendationImpression.exposed_at >= start.replace(tzinfo=None),
            RecommendationImpression.exposed_at < now.replace(tzinfo=None),
        )
        .group_by(RecommendationImpression.target_id)
        .all()
    )
    return {str(target_id): int(count) for target_id, count in rows}


def recent_user_exposures(
    db: Session,
    *,
    viewer_userid: str,
    target_type: str,
    candidate_ids: Iterable[str | int],
    request_now_utc: datetime,
    cooldown_hours: int,
) -> dict[str, datetime]:
    ids = [int(item) for item in candidate_ids]
    if not ids or cooldown_hours <= 0:
        return {}
    now = request_now_utc.astimezone(timezone.utc)
    start = now - timedelta(hours=cooldown_hours)
    rows = (
        db.query(
            RecommendationImpression.target_id,
            func.max(RecommendationImpression.exposed_at),
        )
        .filter(
            RecommendationImpression.viewer_userid == viewer_userid,
            RecommendationImpression.target_type == target_type,
            RecommendationImpression.target_id.in_(ids),
            RecommendationImpression.exposed_at >= start.replace(tzinfo=None),
            RecommendationImpression.exposed_at < now.replace(tzinfo=None),
        )
        .group_by(RecommendationImpression.target_id)
        .all()
    )
    return {str(target_id): exposed_at.replace(tzinfo=timezone.utc) for target_id, exposed_at in rows}


def derive_impressions(db: Session, delivery: RecommendationDelivery, *, exposed_at: datetime | None = None) -> int:
    """Idempotently materialize all items from a sent delivery."""
    if delivery.status != "sent":
        return 0
    context = delivery.recommendation_context or {}
    items = context.get("items") or []
    exposed_at = exposed_at or datetime.now(timezone.utc)
    inserted = 0
    for item in items:
        target_type = item.get("target_type")
        target_id = int(item["target_id"])
        exists = db.query(RecommendationImpression.id).filter(
            RecommendationImpression.delivery_id == delivery.delivery_id,
            RecommendationImpression.target_type == target_type,
            RecommendationImpression.target_id == target_id,
        ).first()
        if exists:
            continue
        db.add(RecommendationImpression(
            delivery_id=delivery.delivery_id,
            request_id=delivery.request_id,
            snapshot_id=delivery.snapshot_id or "",
            viewer_userid=delivery.userid,
            direction=context.get("direction", ""),
            target_type=target_type,
            target_id=target_id,
            position=int(item.get("position", 0)),
            strategy_version_id=context.get("strategy_version_id"),
            algorithm_version=context.get("algorithm_version", "legacy"),
            assignment=context.get("assignment", "legacy"),
            is_exploration=bool(item.get("is_exploration", False)),
            query_digest=context.get("query_digest", ""),
            score_detail=item.get("score_detail"),
            exposed_at=exposed_at.replace(tzinfo=None),
        ))
        inserted += 1
    delivery.impression_actual_count = len(items)
    delivery.impression_expected_count = len(items)
    delivery.impression_state = "completed"
    delivery.impression_derived_at = exposed_at
    return inserted

