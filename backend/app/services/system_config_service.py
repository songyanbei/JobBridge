"""系统配置管理 service（Phase 5 模块 G）。

- 列表按 key 前缀分组
- 单项更新带类型校验
- 危险项变更写 audit_log
- 更新后立即清除 Redis config_cache
"""
from __future__ import annotations

import json
from collections import defaultdict

from sqlalchemy.orm import Session

from app.core.exceptions import BusinessException
from app.core.redis_client import invalidate_config_cache
from app.models import SystemConfig
from app.services.admin_log_service import write_admin_log


DANGER_KEYS = {
    "filter.enable_gender",
    "filter.enable_age",
    "filter.enable_ethnicity",
    "llm.provider",
}

# 不允许通过 admin 接口暴露的 key 前缀（防止 .env 秘钥泄漏）
_HIDDEN_KEYS: set[str] = set()


def list_grouped(db: Session) -> dict:
    rows = db.query(SystemConfig).order_by(SystemConfig.config_key).all()
    grouped: dict[str, list] = defaultdict(list)
    for it in rows:
        if it.config_key in _HIDDEN_KEYS:
            continue
        prefix = it.config_key.split(".")[0] if "." in it.config_key else it.config_key
        grouped[prefix].append({
            "config_key": it.config_key,
            "config_value": it.config_value,
            "value_type": it.value_type,
            "description": it.description,
            "updated_at": it.updated_at.isoformat() if it.updated_at else None,
            "updated_by": it.updated_by,
            "danger": it.config_key in DANGER_KEYS,
        })
    return dict(grouped)


def _validate_value(value_type: str, value: str) -> None:
    if value_type == "int":
        try:
            int(value)
        except (TypeError, ValueError) as exc:
            raise BusinessException(40101, "config_value 必须是整数") from exc
    elif value_type == "bool":
        if str(value).lower() not in ("true", "false", "1", "0"):
            raise BusinessException(40101, "config_value 必须是 true/false")
    elif value_type == "json":
        try:
            json.loads(value)
        except Exception as exc:
            raise BusinessException(40101, "config_value 必须是合法 JSON") from exc
    elif value_type not in ("string",):
        raise BusinessException(40101, f"不支持的 value_type: {value_type}")


def update(
    db: Session,
    key: str,
    new_value: str,
    value_type_override: str | None,
    operator: str,
) -> dict:
    item = db.query(SystemConfig).filter(SystemConfig.config_key == key).first()
    if not item:
        raise BusinessException(40401, f"配置项 {key} 不存在")

    effective_type = value_type_override or item.value_type
    _validate_value(effective_type, new_value)

    before = {
        "config_value": item.config_value,
        "value_type": item.value_type,
    }
    item.config_value = new_value
    if value_type_override:
        item.value_type = value_type_override
    item.updated_by = operator

    is_danger = key in DANGER_KEYS
    # 按 phase5-main §3.1："所有写操作必须写 audit_log"
    write_admin_log(
        db,
        target_type="user", target_id=f"system_config:{key}",
        action="manual_edit", operator=operator,
        before=before,
        after={"config_value": item.config_value, "value_type": item.value_type},
        reason="danger_config_change" if is_danger else "config_change",
    )
    db.commit()

    try:
        invalidate_config_cache(key)
    except Exception:
        pass

    return {
        "changed": before["config_value"] != item.config_value,
        "danger": is_danger,
        "notice": "该配置变更将立即影响业务，请确认" if is_danger else None,
    }
