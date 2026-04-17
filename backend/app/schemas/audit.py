"""审核工作台 DTO（Phase 5 模块 C）。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


TargetType = Literal["job", "resume"]
RiskLevel = Literal["low", "mid", "high"]


class AuditQueueItem(BaseModel):
    """审核队列列表项。"""
    id: int = Field(..., description="岗位或简历主键 id")
    target_type: TargetType = Field(..., description="job 或 resume")
    owner_userid: str = Field(..., description="提交者 external_userid")
    audit_status: str = Field(..., description="pending / passed / rejected")
    risk_level: RiskLevel = Field(default="low", description="low=无风险命中 / mid=灰度 / high=需驳回")
    extracted_brief: str = Field(default="", description="raw_text 摘要（前 120 字）")
    locked_by: str | None = Field(default=None, description="当前软锁持有者 username；null 表示未锁定")
    created_at: datetime
    version: int = Field(..., description="乐观锁版本号；编辑 / pass / reject 必须回带")

    model_config = {"from_attributes": True}


class AuditDetail(BaseModel):
    """审核详情 DTO。"""
    id: int
    target_type: TargetType
    version: int = Field(..., description="乐观锁版本号")
    owner_userid: str
    raw_text: str = Field(..., description="提交者原始文本")
    description: str | None = Field(default=None, description="LLM 清洗后的规范化描述")
    extracted_fields: dict[str, Any] = Field(
        default_factory=dict,
        description="已入库字段快照（含 audit_status / version / expires_at 等）",
    )
    field_confidence: dict[str, float] = Field(
        default_factory=dict,
        description="各字段抽取置信度，0~1；来源为 extra.field_confidence",
    )
    risk_level: RiskLevel = Field(default="low")
    trigger_rules: list[str] = Field(
        default_factory=list,
        description="触发的审核规则，如 ['敏感词:传销']",
    )
    submitter_history: dict[str, Any] = Field(
        default_factory=dict,
        description="近 7 天该提交者审核历史 {passed, rejected, last_7d:{job, resume}}",
    )
    locked_by: str | None = None
    audit_status: str
    audit_reason: str | None = None
    audited_by: str | None = None
    audited_at: datetime | None = None
    created_at: datetime
    expires_at: datetime | None = None
    images: list[str] | None = Field(default=None, description="附带图片 OSS key 数组")


class PassRequest(BaseModel):
    version: int = Field(..., ge=1, description="当前版本号，不一致返回 40902")


class RejectRequest(BaseModel):
    version: int = Field(..., ge=1, description="当前版本号")
    reason: str = Field(..., min_length=1, max_length=255, description="驳回理由，必填")
    notify: bool = Field(default=False, description="是否通知提交者（预留字段，一期不发推送）")
    block_user: bool = Field(default=False, description="是否同时封禁提交者账号")


class EditRequest(BaseModel):
    version: int = Field(..., ge=1, description="当前版本号")
    fields: dict[str, Any] = Field(
        default_factory=dict,
        description="允许白名单字段的变更；未列入白名单的字段会返回 40101",
    )


class PendingCount(BaseModel):
    job: int = Field(..., description="岗位待审条数")
    resume: int = Field(..., description="简历待审条数")
    total: int = Field(..., description="两者合计")
