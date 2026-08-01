"""事件回传 DTO（Phase 5 模块 J，v0.7 §9.9）。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class MiniProgramClickRequest(BaseModel):
    """小程序点击回传入参。

    `delivery_id/request_id/snapshot_id/position/client_event_id` 全部可选：
    新版详情链接至少携带 `delivery_id` 才能归因，老客户端一个都不传也能正常上报，
    只是会被标记为 `legacy_unattributed`（§9.9）。
    """

    userid: str = Field(..., min_length=1, max_length=64, description="external_userid")
    target_type: Literal["job", "resume"] = Field(..., description="点击目标类型")
    target_id: int = Field(..., ge=1, description="目标主键")
    timestamp: int | None = Field(
        default=None,
        description=(
            "客户端事件时间，单位秒或毫秒（> 10^12 视为毫秒自动换算），按 UTC 保存。"
            "缺省取服务端 now。"
        ),
        examples=[1700000000],
    )
    delivery_id: str | None = Field(
        default=None, min_length=36, max_length=36,
        description="推荐投递 ID；带上才走归因链路",
    )
    request_id: str | None = Field(
        default=None, min_length=36, max_length=36,
        description="推荐请求 ID；带上则与投递交叉核对",
    )
    snapshot_id: str | None = Field(
        default=None, min_length=36, max_length=36,
        description="搜索快照 ID；带上则与投递交叉核对",
    )
    # 只是防御性上界：真正落库的 position 由服务端从曝光事实反查，
    # 这里不收敛到 v1 的 Top 3，免得历史 `match.top_n` > 3 的投递被 422 掉。
    position: int | None = Field(
        default=None, ge=1, le=50, description="回复中的位置（服务端以曝光事实为准）",
    )
    client_event_id: str | None = Field(
        default=None, max_length=64, description="新客户端事件幂等 ID",
    )


class MiniProgramClickResponse(BaseModel):
    deduped: bool = Field(..., description="true 表示命中幂等（时间窗或归因唯一键）未重复写库")
    attribution_status: str = Field(
        ..., description="attributed / legacy_unattributed",
    )
