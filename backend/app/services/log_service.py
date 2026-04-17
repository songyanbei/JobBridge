"""对话日志查询 service（Phase 5 模块 I）。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.exceptions import BusinessException
from app.models import ConversationLog


def list_conversations(
    db: Session,
    userid: str,
    start: datetime,
    end: datetime,
    direction: str | None = None,
    intent: str | None = None,
    page: int = 1,
    size: int = 50,
) -> tuple[list[ConversationLog], int]:
    if not userid:
        raise BusinessException(40101, "必须指定 userid")
    if (end - start).days > 30:
        raise BusinessException(40101, "时间范围最大 30 天")
    if end <= start:
        raise BusinessException(40101, "end 必须晚于 start")

    page = max(1, page)
    size = max(1, min(size, 200))

    query = db.query(ConversationLog).filter(
        ConversationLog.userid == userid,
        ConversationLog.created_at >= start,
        ConversationLog.created_at < end,
    )
    if direction:
        if direction not in ("in", "out"):
            raise BusinessException(40101, "无效的 direction")
        query = query.filter(ConversationLog.direction == direction)
    if intent:
        query = query.filter(ConversationLog.intent == intent)

    total = query.count()
    rows = (
        query.order_by(ConversationLog.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )
    return rows, total


def export_conversations(
    db: Session,
    userid: str,
    start: datetime,
    end: datetime,
    direction: str | None = None,
    intent: str | None = None,
    limit: int = 10000,
) -> list[ConversationLog]:
    if (end - start).days > 30:
        raise BusinessException(40101, "时间范围最大 30 天")
    query = db.query(ConversationLog).filter(
        ConversationLog.userid == userid,
        ConversationLog.created_at >= start,
        ConversationLog.created_at < end,
    )
    if direction:
        query = query.filter(ConversationLog.direction == direction)
    if intent:
        query = query.filter(ConversationLog.intent == intent)

    count = query.count()
    if count > limit:
        raise BusinessException(40101, f"导出条数超过上限 {limit}，请缩小时间范围")
    return query.order_by(ConversationLog.created_at.asc()).all()
