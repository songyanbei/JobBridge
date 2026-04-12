"""用户相关 DTO。"""
from datetime import datetime

from pydantic import BaseModel, Field


class UserBase(BaseModel):
    """用户公共字段。"""
    role: str = Field(..., description="角色：worker/factory/broker")
    display_name: str | None = None
    company: str | None = None
    contact_person: str | None = None
    phone: str | None = None


class UserCreate(UserBase):
    """创建用户。"""
    external_userid: str = Field(..., max_length=64, description="企微外部联系人 ID")


class UserUpdate(BaseModel):
    """更新用户（所有字段可选）。"""
    display_name: str | None = None
    company: str | None = None
    contact_person: str | None = None
    phone: str | None = None
    can_search_jobs: bool | None = None
    can_search_workers: bool | None = None
    status: str | None = None
    blocked_reason: str | None = None


class UserRead(BaseModel):
    """用户输出 DTO。"""
    external_userid: str
    role: str
    display_name: str | None = None
    company: str | None = None
    contact_person: str | None = None
    phone: str | None = None
    can_search_jobs: bool = False
    can_search_workers: bool = False
    status: str = "active"
    blocked_reason: str | None = None
    registered_at: datetime
    last_active_at: datetime | None = None
    extra: dict | None = None

    model_config = {"from_attributes": True}
