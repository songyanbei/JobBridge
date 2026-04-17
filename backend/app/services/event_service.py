"""事件回传 service（Phase 5 模块 J）。

记录小程序点击等外部事件：
- 幂等：同一 (userid, target_type, target_id) 在 `event.dedupe_window_seconds` 内视为重复
- 失败降级：event_log 写入失败时记 audit_log，不阻塞业务回包
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.redis_client import (
    EVENT_DEDUPE_TTL_DEFAULT,
    clear_event_idem,
    mark_event_idem,
)
from app.models import EventLog, SystemConfig
from app.services.admin_log_service import write_admin_log

logger = logging.getLogger(__name__)


def _get_dedupe_ttl(db: Session) -> int:
    cfg = db.query(SystemConfig).filter(
        SystemConfig.config_key == "event.dedupe_window_seconds",
    ).first()
    if not cfg:
        return EVENT_DEDUPE_TTL_DEFAULT
    try:
        return int(cfg.config_value)
    except (TypeError, ValueError):
        return EVENT_DEDUPE_TTL_DEFAULT


def record_click(
    db: Session,
    userid: str,
    target_type: str,
    target_id: int,
    timestamp: int | None = None,
) -> bool:
    """记录一次小程序点击事件。

    返回值：
    - True  → 命中去重窗口，已去重（不重复写库）
    - False → 首次写入（或幂等键标记成功但 DB 写入失败后已降级）
    """
    ttl = _get_dedupe_ttl(db)

    # 1. 幂等 key
    try:
        first = mark_event_idem(userid, target_type, target_id, ttl=ttl)
    except Exception:
        logger.exception("event_service: redis mark_event_idem failed (fallback to DB write)")
        first = True  # fail-open，允许写库

    if not first:
        return True  # 已在窗口内

    # 兼容客户端既可能发秒（UNIX 常规）也可能发毫秒（JS Date.now()）：
    # 大于 10^12 视为毫秒，除以 1000 规整为秒后再转 datetime。
    if timestamp:
        ts = int(timestamp)
        if ts > 10 ** 12:
            ts = ts // 1000
        try:
            occurred_at = datetime.fromtimestamp(ts)
        except (OSError, OverflowError, ValueError):
            occurred_at = datetime.now()
    else:
        occurred_at = datetime.now()

    # 2. 写 event_log
    try:
        entry = EventLog(
            event_type="miniprogram_click",
            userid=userid,
            target_type=target_type,
            target_id=target_id,
            occurred_at=occurred_at,
        )
        db.add(entry)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.exception("event_service: event_log write failed userid=%s", userid)
        db.rollback()
        # 释放幂等 key，允许下次同事件重试写库
        try:
            clear_event_idem(userid, target_type, target_id)
        except Exception:
            logger.exception("event_service: clear_event_idem after DB failure failed")
        # 失败兜底：写 audit_log，不阻塞响应
        try:
            write_admin_log(
                db,
                target_type="user", target_id=userid,
                action="auto_reject", operator="system",
                before=None,
                after={"target_type": target_type, "target_id": target_id},
                reason=f"event_log write failed: {exc}",
            )
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("event_service: fallback audit_log write failed")
    return False
