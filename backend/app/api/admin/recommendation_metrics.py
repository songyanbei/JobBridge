"""推荐指标路由（§11.9）。

本层只做参数校验和序列化，复杂查询全部在
``app.services.recommendation_metrics_service``。时间边界统一走
``app.core.time_utils``，不再自己拼 ``datetime.now(timezone.utc)``——那会与 worker
写入口径和 DB ``NOW(6)`` 形成第三套时间口径（§9.12）。
"""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin_role
from app.core.exceptions import BusinessException
from app.core.responses import ok
from app.core.time_utils import business_date
from app.models import AdminUser
from app.services import recommendation_metrics_service

router = APIRouter(prefix="/admin/recommendation-metrics", tags=["admin-recommendation"])

ALLOWED_DIRECTIONS = ("search_job", "search_worker")
ALLOWED_TARGET_TYPES = ("job", "resume")


def _validate_direction(direction: str | None) -> str | None:
    if direction and direction not in ALLOWED_DIRECTIONS:
        raise BusinessException(40101, "无效的 direction")
    return direction


@router.get("", summary="推荐指标总览")
def metrics(
    direction: str | None = Query(default=None, description="search_job / search_worker"),
    days: int = Query(default=7, ge=1, le=recommendation_metrics_service.MAX_WINDOW_DAYS),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin_role("viewer", "operator", "super_admin")),
):
    data = recommendation_metrics_service.collect_metrics(
        db, direction=_validate_direction(direction), days=days,
    ).model_dump()
    # 既有后台卡片读的是顶层扁平标量（requests/impressions/clicks/ctr），保留这
    # 组契约；请求级分组因此让位改名为 request_metrics，其余分组原样下发。
    request_metrics = data.pop("requests")
    data["request_metrics"] = request_metrics
    data.update({
        "requests": request_metrics["total"],
        "exposed_users": data["exposure"]["exposed_users"],
        "impressions": data["exposure"]["impressions"],
        "clicks": data["clicks"]["attributed_impression_clicks"],
        # 历史契约里 ctr 是百分数，分组内的 clicks.ctr 才是 0~1 小数。
        "ctr": round(data["clicks"]["ctr"] * 100.0, 4),
        "unique_candidates": data["exposure"]["exposed_candidates"],
        "zero_result_rate": request_metrics["zero_result_rate"],
        "window_days": days,
        "direction": direction,
    })
    return ok(data)


@router.get("/exposure-daily", summary="自然日曝光聚合")
def exposure_daily(
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = Query(default=None),
    target_type: str | None = Query(default=None, description="job / resume"),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin_role("viewer", "operator", "super_admin")),
):
    if target_type and target_type not in ALLOWED_TARGET_TYPES:
        raise BusinessException(40101, "无效的 target_type")
    # stat_date 是 Asia/Shanghai 自然日（§9.12），默认窗口按业务日回退 30 天。
    end = to or business_date()
    start = from_ or (end - timedelta(days=29))
    if end < start:
        raise BusinessException(40101, "to 不能早于 from")
    if (end - start).days > recommendation_metrics_service.MAX_WINDOW_DAYS:
        raise BusinessException(40101, "时间范围不能超过 90 天")
    return ok(recommendation_metrics_service.get_exposure_daily(
        db, start=start, end=end, target_type=target_type,
    ).model_dump())
