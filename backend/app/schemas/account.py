"""账号管理 DTO（Phase 5 模块 D）。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


Role = Literal["worker", "factory", "broker"]


class UserAdminRead(BaseModel):
    external_userid: str = Field(..., description="企微 external_userid；预注册账号格式 pre_<role>_<8 位 hex>")
    role: Role = Field(..., description="worker / factory / broker")
    display_name: str | None = None
    company: str | None = Field(default=None, description="公司名（厂家/中介）")
    contact_person: str | None = None
    phone: str | None = None
    can_search_jobs: bool = Field(default=False, description="是否允许检索岗位（中介双向标记）")
    can_search_workers: bool = Field(default=False, description="是否允许检索工人（中介双向标记）")
    status: str = Field(..., description="active / blocked / deleted")
    blocked_reason: str | None = Field(default=None, description="封禁理由")
    registered_at: datetime
    last_active_at: datetime | None = None

    model_config = {"from_attributes": True}


class FactoryCreate(BaseModel):
    display_name: str | None = Field(default=None, max_length=64, description="显示名")
    company: str | None = Field(default=None, max_length=128, description="公司名")
    contact_person: str | None = Field(default=None, max_length=64, description="联系人")
    phone: str | None = Field(default=None, max_length=32, description="联系电话")
    external_userid: str | None = Field(
        default=None, max_length=64,
        description="可选：指定 external_userid；未指定则后端生成 pre_factory_<hex>",
    )


class BrokerCreate(FactoryCreate):
    can_search_jobs: bool = Field(default=True, description="中介默认可检索岗位")
    can_search_workers: bool = Field(default=True, description="中介默认可检索工人")


class FactoryUpdate(BaseModel):
    display_name: str | None = None
    company: str | None = None
    contact_person: str | None = None
    phone: str | None = None
    external_userid: str | None = Field(
        default=None,
        description="一期不可修改；传入非当前值会返回 40101",
    )


class BrokerUpdate(FactoryUpdate):
    can_search_jobs: bool | None = None
    can_search_workers: bool | None = None


class BlockRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=255, description="封禁理由，必填")
    notify_user: bool = Field(default=False, description="是否通知用户；一期仅预留字段")


class UnblockRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=255, description="解封理由，必填")


class ImportResult(BaseModel):
    success_count: int = Field(..., description="成功导入行数；任意一行失败则为 0（全量回滚）")
    failed: list[dict] = Field(
        default_factory=list,
        description="失败明细 [{row, error}]；任意一行失败时包含所有问题行",
    )
