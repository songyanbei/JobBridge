"""字典 DTO（Phase 5 模块 F）。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 城市
# ---------------------------------------------------------------------------

class CityRead(BaseModel):
    id: int
    code: str
    name: str
    short_name: str | None = None
    province: str
    aliases: list[str] | None = None
    enabled: bool = True
    updated_at: datetime

    model_config = {"from_attributes": True}


class CityAliasesUpdate(BaseModel):
    aliases: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 工种
# ---------------------------------------------------------------------------

class JobCategoryRead(BaseModel):
    id: int
    code: str
    name: str
    aliases: list[str] | None = None
    sort_order: int = 0
    enabled: bool = True
    updated_at: datetime

    model_config = {"from_attributes": True}


class JobCategoryCreate(BaseModel):
    code: str = Field(..., max_length=32)
    name: str = Field(..., max_length=32)
    aliases: list[str] | None = None
    sort_order: int = 0


class JobCategoryUpdate(BaseModel):
    name: str | None = None
    aliases: list[str] | None = None
    sort_order: int | None = None
    enabled: bool | None = None


# ---------------------------------------------------------------------------
# 敏感词
# ---------------------------------------------------------------------------

SensitiveLevel = Literal["high", "mid", "low"]


class SensitiveWordRead(BaseModel):
    id: int
    word: str
    level: SensitiveLevel
    category: str | None = None
    enabled: bool = True
    created_at: datetime

    model_config = {"from_attributes": True}


class SensitiveWordCreate(BaseModel):
    word: str = Field(..., min_length=1, max_length=64)
    level: SensitiveLevel = "mid"
    category: str | None = None


class SensitiveWordBatchCreate(BaseModel):
    words: list[str] = Field(..., min_length=1)
    level: SensitiveLevel = "mid"
    category: str | None = None
