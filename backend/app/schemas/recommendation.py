"""Recommendation v1 public contracts.

The module intentionally contains no service imports so it can be used by the
search, worker, admin and reporting layers without introducing cycles.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Direction = Literal["search_job", "search_worker"]
Assignment = Literal["legacy", "stable", "candidate"]
ExecutionMode = Literal["off", "shadow", "on"]

#: §9.4 `recommendation_request.request_kind` 的合法取值。
RequestKind = Literal[
    "initial_search", "auto_relaxed", "confirmed_relaxed", "show_more"
]
#: §9.5 `recommendation_search_attempt.attempt_kind` 的合法取值。
#: 注意没有 ``show_more``——show_more 复用父 request 的 served attempt，不产生新 attempt。
AttemptKind = Literal[
    "initial", "relax_probe", "auto_relaxed", "confirmed_relaxed", "shadow_candidate"
]
#: §9.5 `recommendation_search_attempt.llm_status` 的合法取值（没有 ``completed``）。
LlmAttemptStatus = Literal["ok", "timeout", "http_error", "parse_failed", "skipped"]

#: request_kind → attempt_kind。show_more 正常不建 attempt；只有父 request 丢失、
#: 不得不重新物化候选池时才落到这里的 ``initial``。
ATTEMPT_KIND_BY_REQUEST_KIND: dict[str, str] = {
    "initial_search": "initial",
    "auto_relaxed": "auto_relaxed",
    "confirmed_relaxed": "confirmed_relaxed",
    "show_more": "initial",
}

#: §9.6 `recommendation_delivery.status` 的合法取值。
DELIVERY_STATUSES: tuple[str, ...] = (
    "prepared", "pending", "sending", "retry_wait",
    "sent", "permanent_failed", "unknown",
)


class StrategyTemplate(str, Enum):
    BALANCED = "balanced"
    MATCH_FIRST = "match_first"
    EXPOSURE_BALANCED = "exposure_balanced"


class DiversityLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


TEMPLATE_DEFAULTS: dict[str, dict[str, Any]] = {
    StrategyTemplate.BALANCED.value: {
        "match_weight": 70, "quality_weight": 10, "freshness_weight": 8,
        "exposure_weight": 12, "diversity_level": "medium",
        "exploration_percentage": 20, "repeat_cooldown_hours": 24,
        "same_owner_top_n_limit": 1,
    },
    StrategyTemplate.MATCH_FIRST.value: {
        "match_weight": 85, "quality_weight": 8, "freshness_weight": 5,
        "exposure_weight": 2, "diversity_level": "low",
        "exploration_percentage": 5, "repeat_cooldown_hours": 12,
        "same_owner_top_n_limit": 2,
    },
    StrategyTemplate.EXPOSURE_BALANCED.value: {
        "match_weight": 65, "quality_weight": 10, "freshness_weight": 5,
        "exposure_weight": 20, "diversity_level": "high",
        "exploration_percentage": 30, "repeat_cooldown_hours": 72,
        "same_owner_top_n_limit": 1,
    },
}


class RecommendationStrategyParameters(BaseModel):
    """The only tunable v1 business parameters."""

    model_config = ConfigDict(extra="forbid")

    match_weight: int = Field(default=70, ge=60, le=85)
    quality_weight: int = Field(default=15, ge=5, le=15)
    freshness_weight: int = Field(default=10, ge=0, le=15)
    exposure_weight: int = Field(default=5, ge=0, le=20)
    diversity_level: DiversityLevel = "medium"
    exploration_percentage: int = Field(default=10, ge=0, le=30)
    repeat_cooldown_hours: int = Field(default=24, ge=0, le=168)
    same_owner_top_n_limit: int = Field(default=1, ge=1, le=3)

    @model_validator(mode="after")
    def validate_weights(self) -> "RecommendationStrategyParameters":
        if (
            self.match_weight
            + self.quality_weight
            + self.freshness_weight
            + self.exposure_weight
            != 100
        ):
            raise ValueError(
                "match_weight + quality_weight + freshness_weight + exposure_weight must equal 100"
            )
        return self

    @classmethod
    def from_template(cls, template: str) -> "RecommendationStrategyParameters":
        try:
            return cls.model_validate(TEMPLATE_DEFAULTS[template])
        except KeyError as exc:
            raise ValueError(f"unknown recommendation template: {template}") from exc


class RecommendationStrategyVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    direction: Direction
    version_no: int
    template_key: str
    status: Literal["draft", "published", "archived"]
    parameters: RecommendationStrategyParameters
    parameters_digest: str
    last_simulated_digest: str | None = None
    algorithm_version: str = "recommendation-v1"
    base_version_id: int | None = None
    lock_version: int = 1
    change_reason: str
    created_by: str
    created_at: datetime
    published_by: str | None = None
    published_at: datetime | None = None


class RecommendationStrategyDraftUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parameters: RecommendationStrategyParameters
    template_key: str = Field(min_length=1, max_length=32)
    change_reason: str = Field(min_length=1, max_length=255)
    lock_version: int = Field(ge=1)


class RecommendationReleaseRead(BaseModel):
    direction: Direction
    execution_mode: Literal["off", "shadow", "on"]
    stable_version_id: int | None = None
    candidate_version_id: int | None = None
    rollout_percentage: int = Field(ge=0, le=100)
    revision: int
    lock_version: int
    updated_by: str
    updated_at: datetime


class RecommendationReleaseUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_mode: Literal["off", "shadow", "on"]
    candidate_version_id: int | None = None
    rollout_percentage: int = Field(ge=0, le=100)
    lock_version: int = Field(ge=1)
    change_reason: str = Field(min_length=1, max_length=255)


class RecommendationRollbackRequest(BaseModel):
    target_revision: int = Field(ge=1)
    lock_version: int = Field(ge=1)
    change_reason: str = Field(min_length=1, max_length=255)


class RecommendationPromoteRequest(BaseModel):
    lock_version: int = Field(ge=1)
    change_reason: str = Field(min_length=1, max_length=255)


class RecommendationPublishCandidateRequest(BaseModel):
    """§11.7 要求所有写操作都带 change_reason 和 lock_version。

    ``lock_version`` 锁 draft 行，``release_lock_version`` 锁 release 行——发布
    候选同时会在 §9.3 历史表写一条 ``publish_candidate`` 快照并推进 revision。
    """

    model_config = ConfigDict(extra="forbid")

    lock_version: int = Field(ge=1)
    release_lock_version: int = Field(ge=1)
    change_reason: str = Field(min_length=1, max_length=255)


class RecommendationRuntimeControlUpdate(BaseModel):
    enabled: bool
    lock_version: int = Field(ge=1)
    change_reason: str = Field(min_length=1, max_length=255)


AdminRole = Literal["viewer", "operator", "super_admin"]


class AdminUserCreateRequest(BaseModel):
    """创建管理员时必须显式指定角色（§14.8）。"""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=8, max_length=64)
    role: AdminRole
    display_name: str | None = Field(default=None, max_length=64)
    change_reason: str = Field(min_length=1, max_length=255)


class AdminRoleAssignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: AdminRole
    change_reason: str = Field(min_length=1, max_length=255)


class RecommendationScoreDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    match_score: float = Field(ge=0, le=1)
    quality_score: float = Field(ge=0, le=1)
    freshness_score: float = Field(ge=0, le=1)
    exposure_opportunity: float = Field(ge=0, le=1)
    base_score: float = Field(ge=0, le=1)
    repeat_factor: float = Field(ge=0, le=1)
    repeat_adjusted_score: float = Field(ge=0, le=1)
    is_exploration: bool = False
    reason_codes: list[str] = Field(default_factory=list)


class RecommendationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_type: Literal["job", "resume"]
    target_id: int
    position: int
    owner_userid: str | None = None
    final_score: float = Field(ge=0, le=1)
    is_exploration: bool = False
    reason_codes: list[str] = Field(default_factory=list)
    score_detail: RecommendationScoreDetail | None = None


class StrategyAssignment(BaseModel):
    direction: Direction
    execution_mode: Literal["off", "shadow", "on"]
    assignment: Assignment
    strategy_version_id: int | None = None
    candidate_version_id: int | None = None
    algorithm_version: str = "legacy"
    revision: int = 0


class RecommendationDeliveryContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delivery_id: str
    request_id: str
    snapshot_id: str | None = None
    viewer_userid: str
    direction: Direction
    assignment: Assignment
    strategy_version_id: int | None = None
    algorithm_version: str
    query_digest: str
    items: list[RecommendationItem] = Field(default_factory=list)

    @field_validator("items")
    @classmethod
    def positions_are_contiguous(cls, value: list[RecommendationItem]) -> list[RecommendationItem]:
        expected = list(range(1, len(value) + 1))
        if [item.position for item in value] != expected:
            raise ValueError("recommendation item positions must start at 1 and be contiguous")
        return value


# ---------------------------------------------------------------------------
# §9.6 `recommendation_delivery.recommendation_context` 封闭白名单
# ---------------------------------------------------------------------------
# 上面的 ``RecommendationDeliveryContext`` 是**进程内**契约（router → worker），可以
# 带 delivery_id/viewer_userid/owner_userid；下面这组常量和 ``project_delivery_context``
# 才是**落库**口径。方案禁止 JSON 列里出现姓名、企业名、电话、地址、原始 query、
# 候选描述、work_experience 或完整回复，因此 viewer_userid、item 级 owner_userid、
# 原始 direction 都不进列（direction 见 §9.4，消费侧从 recommendation_request 取）。

DELIVERY_CONTEXT_KEYS: frozenset[str] = frozenset({
    "strategy_version_id", "algorithm_version", "assignment", "query_digest", "items",
})
DELIVERY_CONTEXT_ITEM_KEYS: frozenset[str] = frozenset({
    "target_type", "target_id", "position",
    "is_exploration", "reason_codes", "final_score", "score_detail",
})
#: score components 必须是数值；reason_codes 是枚举串。
DELIVERY_CONTEXT_SCORE_KEYS: frozenset[str] = frozenset({
    "match_score", "quality_score", "freshness_score", "exposure_opportunity",
    "base_score", "repeat_factor", "repeat_adjusted_score",
    "is_exploration", "reason_codes",
})


def _context_mapping(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return dict(value) if isinstance(value, Mapping) else {}


def _context_number(value: Any) -> float | None:
    #: bool 是 int 的子类，单独排掉，否则 True 会变成 1.0 的“分数”。
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _context_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _context_token(value: Any, limit: int) -> str:
    return str(value)[:limit] if isinstance(value, (str, int)) and not isinstance(value, bool) else ""


def _context_reason_codes(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(code)[:64] for code in value if isinstance(code, str) and code]


def _project_score_detail(value: Any) -> dict[str, Any] | None:
    detail = _context_mapping(value)
    if not detail:
        return None
    projected: dict[str, Any] = {}
    for key in ("match_score", "quality_score", "freshness_score",
                "exposure_opportunity", "base_score", "repeat_factor",
                "repeat_adjusted_score"):
        number = _context_number(detail.get(key))
        if number is not None:
            projected[key] = number
    projected["is_exploration"] = bool(detail.get("is_exploration", False))
    projected["reason_codes"] = _context_reason_codes(detail.get("reason_codes"))
    return projected


def _project_delivery_item(value: Any) -> dict[str, Any]:
    item = _context_mapping(value)
    projected: dict[str, Any] = {
        "target_type": _context_token(item.get("target_type"), 16),
        "target_id": _context_int(item.get("target_id")),
        "position": _context_int(item.get("position")) or 0,
        "is_exploration": bool(item.get("is_exploration", False)),
        "reason_codes": _context_reason_codes(item.get("reason_codes")),
    }
    final_score = _context_number(item.get("final_score"))
    if final_score is not None:
        projected["final_score"] = final_score
    detail = _project_score_detail(item.get("score_detail"))
    if detail is not None:
        projected["score_detail"] = detail
    return projected


def assert_delivery_context_whitelisted(payload: Mapping[str, Any]) -> None:
    """守住 §9.6 的封闭清单，任何多出来的键都直接报错。

    投影函数自己也会调用它，这样以后给 ``RecommendationDeliveryContext``
    加字段时不会有人“顺手”把新字段漏进落库 JSON。
    """
    extra = set(payload) - DELIVERY_CONTEXT_KEYS
    if extra:
        raise ValueError(
            f"recommendation_context has non-whitelisted keys: {sorted(extra)}"
        )
    for item in payload.get("items") or []:
        item_extra = set(item) - DELIVERY_CONTEXT_ITEM_KEYS
        if item_extra:
            raise ValueError(
                f"recommendation_context item has non-whitelisted keys: {sorted(item_extra)}"
            )
        detail_extra = set(item.get("score_detail") or {}) - DELIVERY_CONTEXT_SCORE_KEYS
        if detail_extra:
            raise ValueError(
                f"recommendation_context score_detail has non-whitelisted keys: "
                f"{sorted(detail_extra)}"
            )


def project_delivery_context(context: Any) -> dict[str, Any]:
    """把投递上下文投影成 §9.6 允许落库的最小 JSON。

    只输出白名单键，其余（``delivery_id``/``request_id``/``snapshot_id`` 已经是
    delivery 的列，``viewer_userid``/``owner_userid``/``direction`` 属于清单外）
    一律丢弃。这是 ``recommendation_delivery.recommendation_context`` 的唯一写入口。
    """
    payload = _context_mapping(context)
    raw_items = payload.get("items")
    projected: dict[str, Any] = {
        "assignment": _context_token(payload.get("assignment"), 16),
        "algorithm_version": _context_token(payload.get("algorithm_version"), 32),
        "query_digest": _context_token(payload.get("query_digest"), 16),
        "strategy_version_id": _context_int(payload.get("strategy_version_id")),
        "items": [
            _project_delivery_item(item)
            for item in (raw_items if isinstance(raw_items, (list, tuple)) else [])
        ],
    }
    assert_delivery_context_whitelisted(projected)
    return projected


class RecommendationRequestFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    source_inbound_msg_id: str
    #: ``None`` = 调用方没有显式编号，持久化层退回 ``reply_index``。写死 0 会让同一条
    #: 入站消息的第二个推荐决策撞上 ``(source_inbound_msg_id, request_index)`` 唯一键。
    request_index: int | None = Field(default=None, ge=0)
    request_kind: RequestKind
    parent_request_id: str | None = None
    viewer_userid: str
    direction: Direction
    query_digest: str
    algorithm_version: str
    candidate_count: int = Field(ge=0, le=50)
    candidate_ids: list[str] = Field(default_factory=list)
    precision_pool_ids: list[str] = Field(default_factory=list)
    served_top_ids: list[str] = Field(default_factory=list)
    is_zero_result: bool = False
    total_latency_ms: int = Field(default=0, ge=0)
    #: §7.5 off 只关闭新排序不关闭可观测性：legacy/off/shadow 请求同样写事实，
    #: 此时 served_assignment/algorithm_version 固定 legacy、strategy_version_id 为空。
    execution_mode: ExecutionMode = "off"
    served_assignment: Assignment = "legacy"
    served_strategy_version_id: int | None = None
    candidate_strategy_version_id: int | None = None
    snapshot_id: str | None = None
    result_count: int = Field(default=0, ge=0)
    #: §9.4：仅 show_more 使用，分页耗尽不得写成业务零结果。
    show_more_exhausted: bool = False
    served_owner_count: int = Field(default=0, ge=0)
    served_max_owner_items: int = Field(default=0, ge=0)
    served_exploration_count: int = Field(default=0, ge=0)
    #: §9.5：最终被采用的那次查询尝试的类型；auto_relaxed 与 confirmed_relaxed
    #: 必须区分（系统自动放宽 vs 用户确认放宽）。
    attempt_kind: AttemptKind = "initial"
    #: §9.5：本次请求跑过的放宽探查步；probe attempt 不得被 served_attempt_id 引用。
    relax_probe_steps: list[str] = Field(default_factory=list)
    #: 同一 request 内、served attempt 之外真实执行过的 initial/relax_probe
    #: 查询。持久化层逐条写 recommendation_search_attempt，禁止只留步骤名。
    additional_attempts: list[dict[str, Any]] = Field(default_factory=list)
    #: §9.5 attempt 事实。``criteria_digest`` 是 CHAR(64) 的**有效条件**规范化摘要，
    #: 不是 16 位 ``query_digest``；留空时持久化层按固定域分隔重新算一个 64 位摘要。
    criteria_digest: str = ""
    attempt_no: int = Field(default=0, ge=0)
    #: 本 attempt 固定的 ``request_now_utc``（打分时刻），不是落库时刻。
    scoring_time_utc: datetime | None = None
    llm_status: LlmAttemptStatus = "skipped"
    llm_input_tokens: int | None = Field(default=None, ge=0)
    llm_output_tokens: int | None = Field(default=None, ge=0)
    llm_timeout_budget_ms: int | None = Field(default=None, ge=0)
    llm_retry_count: int = Field(default=0, ge=0)
    ranking_fallback: str | None = None
    ranking_latency_ms: int = Field(default=0, ge=0)
    #: 本 attempt 自己的耗时；``total_latency_ms`` 是整个 request 的耗时。
    attempt_latency_ms: int = Field(default=0, ge=0)


class RecommendationSimulationRequest(BaseModel):
    direction: Direction
    user_id: str | None = None
    raw_query: str = ""
    criteria: dict[str, Any] = Field(default_factory=dict)
    draft_version_id: int


class RecommendationSimulationResponse(BaseModel):
    current: list[RecommendationItem] = Field(default_factory=list)
    draft: list[RecommendationItem] = Field(default_factory=list)
    request_id: str | None = None
    side_effects_written: bool = False
    #: "stable" 或 "legacy"；stable_version_id=NULL 是合法的 legacy 对照（§7.2），
    #: 对照侧必须给出 legacy 排序而不是空列表。
    current_basis: Literal["stable", "legacy"] = "legacy"
    #: 正常为真实 LLM；调用失败时明确标记 deterministic_fallback。
    simulation_mode: Literal[
        "llm", "deterministic_fallback", "no_candidates"
    ] = "llm"
    llm_invoked: bool = False
    call_site: str = "recommendation_simulation"
    semantic_source: Literal[
        "llm", "llm_fallback_neutral", "no_candidates"
    ] = "no_candidates"
    llm_input_tokens: int | None = Field(default=None, ge=0)
    llm_output_tokens: int | None = Field(default=None, ge=0)
    exposure_available: bool = True
    rank_changes: list[dict[str, Any]] = Field(default_factory=list)
    candidate_summaries: dict[str, Any] = Field(default_factory=dict)


class RecommendationStrategyMetrics(BaseModel):
    model_config = ConfigDict(extra="allow")

    requests: int = 0
    exposed_users: int = 0
    impressions: int = 0
    unique_candidates: int = 0
    owner_concentration: float = 0.0
    duplicate_rate: float = 0.0
    exploration_rate: float = 0.0
    zero_result_rate: float = 0.0
    fallback_rate: float = 0.0
    attributed_click_rate: float = 0.0
