"""数据看板 DTO（Phase 5 模块 H）。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class DayMetrics(BaseModel):
    date: str
    dau_total: int = 0
    dau_worker: int = 0
    dau_factory: int = 0
    dau_broker: int = 0
    uploads_job: int = 0
    uploads_resume: int = 0
    search_count: int = 0
    hit_rate: float = 0.0
    empty_recall_rate: float = 0.0
    audit_pending: int = 0


class DashboardResponse(BaseModel):
    today: DayMetrics
    yesterday: DayMetrics
    trend_7d: list[DayMetrics]


class TrendSeries(BaseModel):
    metric: str
    points: list[dict[str, Any]]


class FunnelStage(BaseModel):
    stage: str
    count: int
