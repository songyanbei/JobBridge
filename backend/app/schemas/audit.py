"""审核工作台 DTO（Phase 5 模块 C）。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


TargetType = Literal["job", "resume"]
RiskLevel = Literal["low", "mid", "high"]


class AuditQueueItem(BaseModel):
    """审核队列列表项。"""
    id: int
    target_type: TargetType
    owner_userid: str
    audit_status: str
    risk_level: RiskLevel = "low"
    extracted_brief: str = ""
    locked_by: str | None = None
    created_at: datetime
    version: int

    model_config = {"from_attributes": True}


class AuditDetail(BaseModel):
    """审核详情 DTO。"""
    id: int
    target_type: TargetType
    version: int
    owner_userid: str
    raw_text: str
    description: str | None = None
    extracted_fields: dict[str, Any] = Field(default_factory=dict)
    field_confidence: dict[str, float] = Field(default_factory=dict)
    risk_level: RiskLevel = "low"
    trigger_rules: list[str] = Field(default_factory=list)
    submitter_history: dict[str, Any] = Field(default_factory=dict)
    locked_by: str | None = None
    audit_status: str
    audit_reason: str | None = None
    audited_by: str | None = None
    audited_at: datetime | None = None
    created_at: datetime
    expires_at: datetime | None = None
    images: list[str] | None = None


class PassRequest(BaseModel):
    version: int = Field(..., ge=1)


class RejectRequest(BaseModel):
    version: int = Field(..., ge=1)
    reason: str = Field(..., min_length=1, max_length=255)
    notify: bool = False
    block_user: bool = False


class EditRequest(BaseModel):
    version: int = Field(..., ge=1)
    fields: dict[str, Any] = Field(default_factory=dict, description="允许白名单字段的变更，value 为新值")


class PendingCount(BaseModel):
    job: int
    resume: int
    total: int
