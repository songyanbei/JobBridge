"""对话相关 DTO。

CandidateSnapshot / SessionState 以方案设计 §11.8 和架构文档 §4.4 为准。
"""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.recommendation import (
    RecommendationDeliveryContext,
    RecommendationRequestFact,
    StrategyAssignment,
)


# ---------------------------------------------------------------------------
# Redis 会话状态 DTO
# ---------------------------------------------------------------------------

class CandidateSnapshot(BaseModel):
    """检索快照（show_more 用），存于 Redis session 内。"""
    candidate_ids: list[str] = Field(default_factory=list, description="Reranker 排序后的完整候选 ID 列表")
    ranking_version: int = Field(default=1, description="每次重新检索 +1")
    query_digest: str = Field(default="", description="search_criteria 的 SHA256 前 12 位")
    created_at: str = Field(default="", description="ISO 8601")
    expires_at: str = Field(default="", description="快照过期时间（created_at + 30 分钟）")
    effective_criteria: dict = Field(
        default_factory=dict,
        description="生成当前候选快照实际使用的检索条件；自动放宽后为放宽后的条件",
    )
    request_id: str | None = None
    snapshot_id: str | None = None
    direction: str | None = None
    strategy_version_id: int | None = None
    algorithm_version: str = "legacy"
    assignment: str = "legacy"
    ranking_metadata: dict = Field(default_factory=dict)


class SessionState(BaseModel):
    """Redis 会话状态，key = session:{external_userid}，TTL 30 分钟。"""
    role: str = Field(..., description="用户角色 worker/factory/broker")
    # Versioned Dialogue/Session metadata.  Defaults preserve read compatibility
    # with Redis payloads written before Phase 1.
    schema_version: str = Field(default="dialogue.v1", description="Dialogue schema version")
    profile: str = Field(default="recruitment.job", description="Active domain profile")
    current_intent: str | None = Field(default=None, description="当前意图")
    search_criteria: dict = Field(default_factory=dict, description="跨轮次累积 merge 的检索条件")
    candidate_snapshot: CandidateSnapshot | None = Field(default=None, description="检索快照")
    shown_items: list[str] = Field(default_factory=list, description="已展示的 ID 集合")
    history: list[dict] = Field(default_factory=list, description='最近 6 轮 [{"role":"user","content":"..."}]')
    updated_at: str = Field(default="", description="ISO 8601")
    session_version: int = Field(
        default=0,
        ge=0,
        description="Redis session 乐观并发版本；每次成功保存递增",
    )
    broker_direction: str | None = Field(default=None, description="中介搜索方向 search_job / search_worker")
    follow_up_rounds: int = Field(default=0, description="上传追问轮数计数，最多 2 轮")

    # ---- Stage A：多轮上传过渡字段（详见 docs/multi-turn-upload-stage-a-implementation.md §3.1） ----
    # 这些字段仅在“上传缺字段”流程中使用，旧 Redis session 反序列化时全部走默认值，不影响兼容性。
    pending_upload: dict = Field(default_factory=dict, description="上传草稿数据：已抽取的结构化字段")
    pending_upload_intent: str | None = Field(default=None, description="原始上传 intent: upload_job/upload_resume/upload_and_search")
    awaiting_field: str | None = Field(default=None, description="当前重点追问的字段名")
    pending_started_at: str | None = Field(default=None, description="草稿创建时间 ISO 8601 UTC")
    pending_updated_at: str | None = Field(default=None, description="草稿最近更新时间 ISO 8601 UTC")
    pending_expires_at: str | None = Field(default=None, description="草稿过期时间 ISO 8601 UTC，默认创建后 10 分钟")
    pending_raw_text_parts: list[str] = Field(default_factory=list, description="多轮原始用户文本，按时间顺序")
    pending_upload_mode: str = Field(default="create", description="create / replace")
    pending_target_id: int | None = Field(default=None)
    pending_target_version: int | None = Field(default=None)
    pending_operation_id: str | None = Field(default=None)
    pending_rollout_cohort: str | None = Field(default=None, pattern="^(enabled|control)$")
    pending_rollout_revision: int | None = Field(default=None, ge=1)
    pending_upload_media_ids: list[int] = Field(default_factory=list)
    attachment_target_type: str | None = Field(
        default=None,
        description="最近一次成功激活的上传实体类型，仅用于后续图片精确挂载",
    )
    attachment_target_id: int | None = Field(
        default=None,
        strict=True,
        gt=0,
        description="与 attachment_target_type 成对的精确实体 ID",
    )

    # ---- Stage C1：兼容式状态机字段（详见 docs/multi-turn-upload-stage-c-implementation.md §2.3） ----
    # 这些字段保留 Stage A/B 扁平字段并存，旧 Redis session 反序列化时全部走默认值。
    active_flow: str | None = Field(
        default=None,
        description="路由裁决源：idle / upload_collecting / upload_conflict / search_active",
    )
    last_intent: str | None = Field(
        default=None,
        description="本轮 LLM 意图记录，仅供观测/日志，不参与路由（与 current_intent 双写期）",
    )
    pending_interruption: dict | None = Field(
        default=None,
        description="upload_conflict 中保存的新意图瘦身版："
                    "{intent, structured_data, criteria_patch, raw_text}",
    )
    failed_patch_rounds: int = Field(
        default=0,
        description="精细失败补字段计数；C1 起作为 max rounds 主退出依据，>=2 清草稿",
    )
    last_criteria: dict = Field(
        default_factory=dict,
        description="最近一次有效搜索的 criteria 快照；不论命中与否都写入，方便后续放宽继承上下文",
    )
    conflict_followup_rounds: int = Field(
        default=0,
        description="upload_conflict 已经追问确认的轮数；超过 1 轮后清草稿回 idle 防死循环",
    )

    # ---- Phase 1（dialogue-intent-extraction-phased-plan §1.3）：搜索 awaiting 物化 ----
    # 搜索流程因为缺字段追问时，把缺失字段写入 FIFO 队列，下一轮裸值优先按字段类型落槽。
    # 与上传草稿的 awaiting_field 完全独立（避免 search/upload 交叉污染）；旧 Redis
    # session 反序列化全部走默认值。
    awaiting_fields: list[str] = Field(
        default_factory=list,
        description="搜索追问的字段 FIFO 队列；按写入顺序消费，消费后从队列移除",
    )
    awaiting_frame: str | None = Field(
        default=None,
        description="awaiting_fields 所属的 frame：job_search / candidate_search；用于跨 frame 隔离",
    )
    awaiting_expires_at: str | None = Field(
        default=None,
        description="搜索 awaiting 过期时间 ISO 8601 UTC；过期后裸值不再按补槽处理",
    )

    # ---- Phase 5 §5.2：跨 turn 放宽确认状态（独立于 upload_conflict 流程） ----
    # 当 reducer 输出 ask_clarification 反问"要把薪资放宽 10% 吗"时，applier 把
    # 待确认上下文写入此字段；下一轮用户回应（accept/reject）由
    # _route_v2_relaxation_response 消费 + 清空。**完全独立**于 pending_interruption
    # （upload_conflict 专用），两条流程零交叉。结构示例：
    # {
    #   "frame": "job_search",
    #   "direction": "search_job",
    #   "step": "relax_salary_10pct",
    #   "original_criteria": {...},  # 主搜索时未放宽 criteria；execute_relaxed_search 第一参数
    #   "relaxed_criteria": {...},   # 反问文案展示用，不参与二次检索（防二次放宽）
    #   "raw_query": "...",          # 主搜索原文，二次 reranker 必须复用，不能用确认轮 msg.content
    #   "user_msg_id": "...",
    #   "expires_at": "...",
    # }
    pending_relaxation: dict | None = Field(
        default=None,
        description="Phase 5 §5.2：跨 turn 放宽确认上下文（与 pending_interruption 独立）",
    )
    pending_action: dict | None = Field(
        default=None,
        description="受限两动作计划的第二动作：{raw_text, created_at, expires_at}",
    )
    # Materialized compatibility payload for legacy readers.  It is refreshed by
    # conversation_service.save_session and ignored by old consumers.
    legacy_projection: dict = Field(default_factory=dict, description="Legacy session projection")

    def get_legacy_projection(self) -> dict:
        """Return the stable pre-Phase-1 session shape."""
        fields = (
            "role", "current_intent", "search_criteria", "candidate_snapshot",
            "shown_items", "history", "updated_at", "session_version",
            "broker_direction", "follow_up_rounds", "pending_upload",
            "pending_upload_intent", "awaiting_field", "pending_started_at",
            "pending_updated_at", "pending_expires_at", "pending_raw_text_parts",
            "pending_upload_mode", "pending_target_id", "pending_target_version",
            "pending_operation_id", "pending_rollout_cohort", "pending_rollout_revision",
            "pending_upload_media_ids", "attachment_target_type", "attachment_target_id",
            "active_flow", "last_intent", "pending_interruption", "failed_patch_rounds",
            "last_criteria", "conflict_followup_rounds", "awaiting_fields",
            "awaiting_frame", "awaiting_expires_at", "pending_relaxation", "pending_action",
        )
        payload = self.model_dump(mode="json", exclude={"legacy_projection"})
        return {key: payload.get(key) for key in fields if key in payload}

    # Naming used by adapters outside the schema module.
    def to_legacy_projection(self) -> dict:
        return self.get_legacy_projection()


class CriteriaPatch(BaseModel):
    """多轮对话的 criteria 增量更新指令。"""
    op: str = Field(..., description="操作类型：add / update / remove")
    field: str = Field(..., description="字段名")
    value: Any = Field(default=None, description="新值")


class ReplyMessage(BaseModel):
    """Phase 4 消息路由层的出站回复 DTO。

    由 message_router / command_service 产出，Worker 负责投递到企微。
    一期固定 text 类型；如果未来支持卡片等扩展类型再在 msg_type 上区分。

    intent 与 criteria_snapshot 非必填；message_router 在搜索/翻页等
    场景会附带当轮 criteria 与 prompt_version，Worker 落库到
    conversation_log.criteria_snapshot，便于后续运营查询。
    """
    userid: str = Field(..., description="接收者 external_userid")
    content: str = Field(..., description="回复文本")
    msg_type: str = Field(default="text", description="消息类型（一期固定 text）")
    intent: str | None = Field(default=None, description="本轮意图（可选，用于日志）")
    criteria_snapshot: dict | None = Field(
        default=None,
        description="本轮 criteria 快照 + prompt_version；落 conversation_log.criteria_snapshot",
    )
    delivery_id: str | None = Field(default=None)
    recommendation_context: RecommendationDeliveryContext | None = Field(default=None)
    session_mutation: dict | None = Field(default=None)
    recommendation_request: RecommendationRequestFact | None = Field(default=None)
    strategy_assignment: StrategyAssignment | None = Field(default=None)


# ---------------------------------------------------------------------------
# 对话日志 DTO
# ---------------------------------------------------------------------------

class ConversationLogCreate(BaseModel):
    """创建对话日志。"""
    userid: str = Field(..., max_length=64)
    direction: str = Field(..., description="in / out")
    msg_type: str = Field(..., description="text / image / voice / system")
    content: str
    wecom_msg_id: str | None = None
    intent: str | None = None
    criteria_snapshot: dict | None = None
    expires_at: datetime


class ConversationLogRead(BaseModel):
    """对话日志输出 DTO。"""
    id: int
    userid: str
    direction: str
    msg_type: str
    content: str
    wecom_msg_id: str | None = None
    intent: str | None = None
    criteria_snapshot: dict | None = None
    created_at: datetime
    expires_at: datetime

    model_config = {"from_attributes": True}
