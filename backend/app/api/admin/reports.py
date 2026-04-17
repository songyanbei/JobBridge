"""数据看板路由（Phase 5 模块 H）。"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.core.csv_export import rows_to_csv_bytes
from app.core.exceptions import BusinessException
from app.core.responses import ok
from app.models import AdminUser
from app.services import report_service

router = APIRouter(prefix="/admin/reports", tags=["admin-reports"])


@router.get("/dashboard", summary="运营看板概览")
def dashboard(
    force_refresh: bool = Query(False),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    return ok(report_service.get_dashboard(db, force_refresh=force_refresh))


@router.get("/trends", summary="趋势数据")
def trends(
    range: str = Query("7d", description="7d / 30d / custom"),
    from_: date | None = Query(None, alias="from"),
    to: date | None = Query(None),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    return ok(report_service.get_trends(db, range, from_, to))


@router.get("/top", summary="TOP 榜单")
def top(
    dim: str = Query(..., description="city / job_category / role"),
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    return ok(report_service.get_top(db, dim, limit))


@router.get("/funnel", summary="转化漏斗")
def funnel(
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    return ok(report_service.get_funnel(db))


@router.get("/export", summary="看板数据导出")
def export_reports(
    metric: str = Query("daily"),
    from_: date | None = Query(None, alias="from"),
    to: date | None = Query(None),
    format: str = Query("csv"),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    if format != "csv":
        raise BusinessException(40101, "仅支持 csv 格式")
    end = to or date.today()
    start = from_ or (end - timedelta(days=29))
    if (end - start).days > 90:
        raise BusinessException(40101, "时间范围不能超过 90 天")

    headers, rows = report_service.export_metric(db, metric, start, end)
    data = rows_to_csv_bytes(headers, rows)
    filename = f"reports_{metric}_{datetime.now().strftime('%Y%m%d%H%M')}.csv"
    return Response(content=data, media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
    })
