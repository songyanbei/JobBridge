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


# ---------------------------------------------------------------------------
# 推荐报表 DTO（§11.9）
#
# 约定：
# - 比率一律是 0~1 的小数，不是百分数；
# - ``None`` 表示"该指标没有事实来源"，与 0（"有来源、值为零"）严格区分；
# - 所有字段都是聚合量，不含 viewer 标识（§9.11）。
# ---------------------------------------------------------------------------

class MetricsWindow(BaseModel):
    days: int
    start_utc: str
    end_utc: str
    direction: str | None = None
    business_timezone: str


class RequestMetrics(BaseModel):
    total: int = 0
    zero_result: int = 0
    business_zero_result: int = 0
    zero_result_rate: float = 0.0
    show_more_exhausted: int = 0
    by_kind: dict[str, int] = {}
    execution_mode_counts: dict[str, int] = {}
    execution_mode_share: dict[str, float] = {}
    assignment_counts: dict[str, int] = {}
    assignment_share: dict[str, float] = {}
    top3_single_owner_rate: float = 0.0
    top3_single_owner_requests: int = 0
    top3_repeat_rate: float = 0.0
    top3_repeat_pairs: int = 0
    total_latency_p95_ms: float | None = None
    total_latency_p99_ms: float | None = None
    truncated: bool = False


class AttemptMetrics(BaseModel):
    total: int = 0
    ranking_attempts: int = 0
    reranker_fallback: int = 0
    reranker_fallback_rate: float = 0.0
    fallback_by_reason: dict[str, int] = {}
    llm_status_counts: dict[str, int] = {}
    llm_status_share: dict[str, float] = {}
    llm_retry_count: int = 0
    zero_candidate_attempts: int = 0
    zero_candidate_rate: float = 0.0
    ranking_latency_p95_ms: float | None = None
    ranking_latency_p99_ms: float | None = None


class ExposureMetrics(BaseModel):
    impressions: int = 0
    exposed_users: int = 0
    exposed_candidates: int = 0
    exploration_impressions: int = 0
    exploration_share: float = 0.0
    #: 只统计出现过曝光的候选。与 ``RecallPoolMetrics.gini`` 口径不同，不得混用。
    exposed_candidate_gini: float = 0.0


class RecallPoolMetrics(BaseModel):
    """全召回池曝光集中度：分母含零曝光候选（§11.9）。"""

    attempts: int = 0
    pool_candidates: int = 0
    exposed_candidates: int = 0
    coverage: float = 0.0
    gini: float = 0.0
    truncated: bool = False


class StrategyCtrPoint(BaseModel):
    strategy_version_id: int | None = None
    impressions: int = 0
    clicks: int = 0
    ctr: float = 0.0


class ClickMetrics(BaseModel):
    attributed_impression_clicks: int = 0
    ctr: float = 0.0
    exploration_clicks: int = 0
    exploration_ctr: float = 0.0
    non_exploration_ctr: float = 0.0
    by_strategy_version: list[StrategyCtrPoint] = []
    attribution_counts: dict[str, int] = {}
    attribution_share: dict[str, float] = {}
    attributed_click_rate: float = 0.0
    #: legacy 点击没有 delivery_id，无法判定方向，归因比例不跟随 direction 过滤。
    attribution_direction_scoped: bool = False


class DeliveryMetrics(BaseModel):
    total: int = 0
    status_counts: dict[str, int] = {}
    status_share: dict[str, float] = {}
    unknown_rate: float = 0.0
    status_age_seconds: dict[str, dict[str, float | None]] = {}
    impression_state_counts: dict[str, int] = {}
    impression_backlog: int = 0
    impression_backlog_rate: float = 0.0
    sent_to_impression_p95_ms: float | None = None
    sent_to_impression_p99_ms: float | None = None
    sent_to_impression_samples: int = 0
    prepared_session_conflicts: int | None = None
    dispatcher_claim_latency_p95_ms: float | None = None
    truncated: bool = False


class LlmCostMetrics(BaseModel):
    legacy_input_tokens: int = 0
    legacy_output_tokens: int = 0
    by_direction: dict[str, dict[str, int]] = {}
    shadow_input_tokens: int = 0
    shadow_output_tokens: int = 0
    daily_cost_by_direction: dict[str, float] | None = None
    provider_throttle_rate: float | None = None


class ShadowMetrics(BaseModel):
    """Shadow 聚合指标。

    ``available`` 表示 request/attempt 上的核心 shadow 事实可查询；
    ``missing_sources`` 单独列出仍只存在于日志等外部系统、无法由本接口可靠聚合的
    指标。缺少单个外部来源不应让其余已落库指标全部消失。
    """

    available: bool = False
    missing_sources: list[str] = []
    requests: int = 0
    top_n_overlap_rate: float | None = None
    average_position_delta: float | None = None
    timeout_count: int = 0
    local_capacity_skip_count: int = 0
    global_capacity_skip_count: int = 0
    persistence_drop_count: int | None = None
    queue_wait_p95_ms: float | None = None
    duration_p95_ms: float | None = None


class RecommendationMetricsResponse(BaseModel):
    window: MetricsWindow
    requests: RequestMetrics
    attempts: AttemptMetrics
    exposure: ExposureMetrics
    recall_pool: RecallPoolMetrics
    clicks: ClickMetrics
    delivery: DeliveryMetrics
    llm: LlmCostMetrics
    shadow: ShadowMetrics


class ExposureDailyPoint(BaseModel):
    stat_date: str
    target_type: str
    candidates: int = 0
    impressions: int = 0
    max_candidate_impressions: int = 0


class ExposureDailyResponse(BaseModel):
    start: str
    end: str
    target_type: str | None = None
    business_timezone: str
    points: list[ExposureDailyPoint] = []
