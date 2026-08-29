"""运营后台 audit_log 写入工具（Phase 5 共享）。

所有 admin 写操作通过本模块写 audit_log，保证：
- operator 必填
- snapshot 结构统一为 {"before": {...}, "after": {...}}
- 不在这里 commit，由调用方显式 commit
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditLog


_ALLOWED_TARGET_TYPES = {"job", "resume", "user", "system", "recommendation_strategy"}
_ALLOWED_ACTIONS = {
    "auto_pass", "auto_reject",
    "manual_pass", "manual_reject",
    "manual_edit", "undo",
    "appeal", "reinstate",
    "strategy_publish", "strategy_rollout", "strategy_promote",
    "strategy_rollback", "strategy_kill_switch",
}


def _json_safe(value: Any) -> Any:
    """将 datetime / date 等类型转换为可 JSON 序列化对象。"""
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    # SQLAlchemy Column / ORM object → 尝试转 str
    try:
        return json.loads(json.dumps(value, default=str, ensure_ascii=False))
    except Exception:
        return str(value)


def write_admin_log(
    db: Session,
    *,
    target_type: str,
    target_id: str | int,
    action: str,
    operator: str,
    before: dict | None = None,
    after: dict | None = None,
    reason: str | None = None,
) -> AuditLog:
    """写入 audit_log。

    - target_type: job / resume / user / system / recommendation_strategy（未知类型规范到 user）
    - snapshot 统一 {"before": ..., "after": ...}
    - 调用方负责 commit
    """
    if not operator:
        raise ValueError("operator 不能为空")
    if action not in _ALLOWED_ACTIONS:
        raise ValueError(f"无效的 audit action: {action}")

    # ``system`` is a first-class audit target used by readiness checks; only
    # truly unknown target types are normalized to ``user``.
    effective_target_type = target_type if target_type in _ALLOWED_TARGET_TYPES else "user"
    effective_reason = reason
    if target_type not in _ALLOWED_TARGET_TYPES:
        note = f"original_target_type={target_type}"
        effective_reason = f"{reason}; {note}" if reason else note

    snapshot: dict | None = None
    if before is not None or after is not None:
        snapshot = {
            "before": _json_safe(before) if before is not None else None,
            "after": _json_safe(after) if after is not None else None,
        }

    entry = AuditLog(
        target_type=effective_target_type,
        target_id=str(target_id),
        action=action,
        reason=(effective_reason or None),
        operator=operator,
        snapshot=snapshot,
    )
    db.add(entry)
    return entry
