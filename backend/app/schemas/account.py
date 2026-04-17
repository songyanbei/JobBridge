"""账号管理 DTO（Phase 5 模块 D）。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


Role = Literal["worker", "factory", "broker"]


class UserAdminRead(BaseModel):
    external_userid: str
    role: Role
    display_name: str | None = None
    company: str | None = None
    contact_person: str | None = None
    phone: str | None = None
    can_search_jobs: bool = False
    can_search_workers: bool = False
    status: str
    blocked_reason: str | None = None
    registered_at: datetime
    last_active_at: datetime | None = None

    model_config = {"from_attributes": True}


class FactoryCreate(BaseModel):
    display_name: str | None = Field(default=None, max_length=64)
    company: str | None = Field(default=None, max_length=128)
    contact_person: str | None = Field(default=None, max_length=64)
    phone: str | None = Field(default=None, max_length=32)
    external_userid: str | None = Field(default=None, max_length=64)


class BrokerCreate(FactoryCreate):
    can_search_jobs: bool = True
    can_search_workers: bool = True


class FactoryUpdate(BaseModel):
    display_name: str | None = None
    company: str | None = None
    contact_person: str | None = None
    phone: str | None = None
    external_userid: str | None = None


class BrokerUpdate(FactoryUpdate):
    can_search_jobs: bool | None = None
    can_search_workers: bool | None = None


class BlockRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=255)
    notify_user: bool = False


class UnblockRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=255)


class ImportResult(BaseModel):
    success_count: int
    failed: list[dict]  # [{"row": int, "error": str}]
