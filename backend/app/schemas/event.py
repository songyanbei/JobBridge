"""事件回传 DTO（Phase 5 模块 J）。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class MiniProgramClickRequest(BaseModel):
    userid: str = Field(..., min_length=1, max_length=64)
    target_type: Literal["job", "resume"]
    target_id: int = Field(..., ge=1)
    timestamp: int | None = Field(default=None, description="客户端事件发生时间（秒），缺省取服务端 now")


class MiniProgramClickResponse(BaseModel):
    deduped: bool
