"""Recommendation v1 public contracts.

The module intentionally contains no service imports so it can be used by the
search, worker, admin and reporting layers without introducing cycles.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Direction = Literal["search_job", "search_worker"]
Assignment = Literal["legacy", "stable", "candidate"]


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


class RecommendationRequestFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    source_inbound_msg_id: str
    request_index: int = 0
    request_kind: Literal[
        "initial_search", "auto_relaxed", "confirmed_relaxed", "show_more"
    ]
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
    #: 模拟走确定性流水线，不调用 LLM；语义分统一取中性值，据实声明避免误读。
    simulation_mode: Literal["deterministic"] = "deterministic"
    llm_invoked: bool = False
    call_site: str = "recommendation_simulation"
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
