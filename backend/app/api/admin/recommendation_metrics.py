from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin_role
from app.core.responses import ok
from app.models import AdminUser, EventLog, RecommendationImpression, RecommendationRequest

router = APIRouter(prefix="/admin/recommendation-metrics", tags=["admin-recommendation"])


@router.get("")
def metrics(
    direction: str | None = Query(default=None),
    days: int = Query(default=7, ge=1, le=90),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin_role("viewer", "operator", "super_admin")),
):
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    request_query = db.query(RecommendationRequest).filter(RecommendationRequest.created_at >= since)
    impression_query = db.query(RecommendationImpression).filter(RecommendationImpression.exposed_at >= since)
    if direction:
        request_query = request_query.filter(RecommendationRequest.direction == direction)
        impression_query = impression_query.filter(RecommendationImpression.direction == direction)
    requests = request_query.count()
    zero = request_query.filter(RecommendationRequest.is_zero_result.is_(True)).count()
    impressions = impression_query.count()
    click_query = db.query(EventLog).filter(
        EventLog.occurred_at >= since,
        EventLog.attribution_status == "attributed",
    )
    clicks = click_query.count()
    users = impression_query.with_entities(func.count(func.distinct(RecommendationImpression.viewer_userid))).scalar() or 0
    return ok({
        "requests": requests,
        "exposed_users": int(users),
        "impressions": impressions,
        "clicks": clicks,
        "ctr": (clicks * 100.0 / impressions) if impressions else 0.0,
        "unique_candidates": int(
            impression_query.with_entities(func.count(func.distinct(
                RecommendationImpression.target_type,
                RecommendationImpression.target_id,
            ))).scalar() or 0
        ),
        "zero_result_rate": zero / requests if requests else 0.0,
        "window_days": days,
        "direction": direction,
    })
