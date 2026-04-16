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
