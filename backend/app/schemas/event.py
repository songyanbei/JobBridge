"""事件回传 DTO（Phase 5 模块 J）。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class MiniProgramClickRequest(BaseModel):
    userid: str = Field(..., min_length=1, max_length=64, description="external_userid")
    target_type: Literal["job", "resume"] = Field(..., description="点击目标类型")
    target_id: int = Field(..., ge=1, description="目标主键")
    timestamp: int | None = Field(
        default=None,
        description=(
            "客户端事件时间，单位秒或毫秒（> 10^12 视为毫秒自动换算）。"
            "缺省取服务端 now。"
        ),
        examples=[1700000000],
    )


class MiniProgramClickResponse(BaseModel):
    deduped: bool = Field(..., description="true 表示命中 10 分钟幂等窗口未重复写库")
