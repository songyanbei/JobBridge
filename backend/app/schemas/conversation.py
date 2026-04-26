"""对话相关 DTO。

CandidateSnapshot / SessionState 以方案设计 §11.8 和架构文档 §4.4 为准。
"""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


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


class SessionState(BaseModel):
    """Redis 会话状态，key = session:{external_userid}，TTL 30 分钟。"""
    role: str = Field(..., description="用户角色 worker/factory/broker")
    current_intent: str | None = Field(default=None, description="当前意图")
    search_criteria: dict = Field(default_factory=dict, description="跨轮次累积 merge 的检索条件")
    candidate_snapshot: CandidateSnapshot | None = Field(default=None, description="检索快照")
    shown_items: list[str] = Field(default_factory=list, description="已展示的 ID 集合")
    history: list[dict] = Field(default_factory=list, description='最近 6 轮 [{"role":"user","content":"..."}]')
    updated_at: str = Field(default="", description="ISO 8601")
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
