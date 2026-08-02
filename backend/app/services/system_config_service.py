"""系统配置管理 service（Phase 5 模块 G）。

- 列表按 key 前缀分组
- 单项更新带类型校验
- 危险项变更写 audit_log
- 更新后立即清除 Redis config_cache
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.exceptions import BusinessException
from app.core.redis_client import invalidate_config_cache
from app.models import SystemConfig
from app.models import AuditLog
from app.services.visibility_contract import (
    FIELD_REGISTRIES, SENSITIVE_EXPANSION_FIELDS, VisibilityScene, ViewerRole,
)
from app.services.visibility_policy import (
    VISIBILITY_POLICY_KEY, NormalizedPolicy,
    VisibilityPolicyValidationError, normalize_policy,
)

LOCKED_RECOMMENDATION_KEYS = {"match.max_candidates", "match.top_n"}
from app.services.admin_log_service import write_admin_log


DANGER_KEYS = {
    "filter.enable_gender",
    "filter.enable_age",
    "filter.enable_ethnicity",
    "llm.provider",
}

# 不允许通过 admin 接口暴露的 key 前缀（防止 .env 秘钥泄漏）
_HIDDEN_KEYS: set[str] = set(LOCKED_RECOMMENDATION_KEYS)
_HIDDEN_KEYS.add(VISIBILITY_POLICY_KEY)


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
    if key in LOCKED_RECOMMENDATION_KEYS or key == VISIBILITY_POLICY_KEY:
        raise ValueError("config_locked_by_recommendation_v1")
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
    # 按 phase5-main §3.1 / §5.6：所有配置变更写 audit_log；
    # target_type 固定为 system，target_id 为 config_key 原值（无前缀），
    # 方便按 system/<key> 条件查询变更历史。
    write_admin_log(
        db,
        target_type="system", target_id=key,
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


def _policy_item(db: Session, *, for_update: bool = False) -> SystemConfig:
    query = db.query(SystemConfig).filter(SystemConfig.config_key == VISIBILITY_POLICY_KEY)
    if for_update:
        query = query.with_for_update()
    item = query.first()
    if not item:
        raise BusinessException(50001, "推荐权限策略配置缺失，请先执行数据库迁移")
    return item


def _policy_document(item: SystemConfig) -> NormalizedPolicy:
    try:
        return normalize_policy(item.config_value)
    except VisibilityPolicyValidationError as exc:
        raise BusinessException(50001, f"当前推荐权限策略非法: {exc}") from exc


def _policy_metadata() -> dict:
    return {
        scene.value: {
            role.value: [
                {
                    "key": field.key, "label": field.label,
                    "sensitive": field.sensitive,
                    "default_visible": role in field.default_visible_roles,
                }
                for field in FIELD_REGISTRIES[scene]
            ]
            for role in ViewerRole
        }
        for scene in VisibilityScene
    }


def _sensitive_additions(before: NormalizedPolicy, after: NormalizedPolicy) -> set[str]:
    added: set[str] = set()
    for scene in VisibilityScene:
        for role in ViewerRole:
            added |= (
                set(after.matrix[scene][role]) - set(before.matrix[scene][role])
            ) & set(SENSITIVE_EXPANSION_FIELDS)
    return added


def get_visibility_policy(db: Session) -> dict:
    item = _policy_item(db)
    policy = _policy_document(item)
    return {
        "config_key": VISIBILITY_POLICY_KEY,
        "schema_version": policy.schema_version,
        "revision": policy.revision,
        "matrix": policy.as_dict(),
        "fields": _policy_metadata(),
        "audit_retention_days": _audit_retention_days(db),
    }


def validate_visibility_policy(db: Session, payload: dict) -> dict:
    current = _policy_document(_policy_item(db))
    try:
        candidate = normalize_policy(payload)
    except VisibilityPolicyValidationError as exc:
        raise BusinessException(40101, str(exc), {"error_code": exc.code}) from exc
    return {
        "valid": True,
        "schema_version": candidate.schema_version,
        "matrix": candidate.as_dict(),
        "sensitive_additions": sorted(_sensitive_additions(current, candidate)),
    }


def _save_visibility_policy(
    db: Session, payload: dict, expected_revision: int,
    operator: str, confirm_sensitive_expansion: bool,
    *, reason: str,
) -> dict:
    item = _policy_item(db, for_update=True)
    current = _policy_document(item)
    if current.revision != expected_revision:
        raise BusinessException(
            40902, "推荐权限策略版本冲突",
            {"current_revision": current.revision},
        )
    candidate_payload = dict(payload)
    candidate_payload["revision"] = current.revision + 1
    try:
        candidate = normalize_policy(candidate_payload)
    except VisibilityPolicyValidationError as exc:
        raise BusinessException(40101, str(exc), {"error_code": exc.code}) from exc
    additions = _sensitive_additions(current, candidate)
    if additions and not confirm_sensitive_expansion:
        raise BusinessException(
            40101, "新增高敏字段必须确认",
            {"sensitive_fields": sorted(additions), "confirm_required": True},
        )
    before_value = current.as_dict()
    item.config_value = json.dumps(candidate.as_dict(), ensure_ascii=False, separators=(",", ":"))
    item.value_type = "json"
    item.updated_by = operator
    write_admin_log(
        db, target_type="system", target_id=VISIBILITY_POLICY_KEY,
        action="manual_edit", operator=operator,
        before={"config_value": before_value, "schema_version": current.schema_version, "revision": current.revision},
        after={"config_value": candidate.as_dict(), "schema_version": candidate.schema_version, "revision": candidate.revision},
        reason=reason,
    )
    db.commit()
    return {"schema_version": candidate.schema_version, "revision": candidate.revision, "matrix": candidate.as_dict()}


def update_visibility_policy(
    db: Session, payload: dict, expected_revision: int, operator: str,
    confirm_sensitive_expansion: bool = False,
) -> dict:
    return _save_visibility_policy(
        db, payload, expected_revision, operator, confirm_sensitive_expansion,
        reason="visibility_policy_update",
    )


def _audit_retention_days(db: Session) -> int:
    item = db.query(SystemConfig).filter(SystemConfig.config_key == "ttl.audit_log.days").first()
    try:
        return max(1, int(item.config_value)) if item else 180
    except (AttributeError, TypeError, ValueError):
        return 180


def _history_entry(row: AuditLog, retention_days: int) -> dict | None:
    snapshot = row.snapshot or {}
    after = snapshot.get("after") if isinstance(snapshot, dict) else None
    if not isinstance(after, dict) or not isinstance(after.get("config_value"), dict):
        return None
    try:
        policy = normalize_policy(after["config_value"])
    except VisibilityPolicyValidationError:
        return None
    created = row.created_at
    age_days = ((datetime.now(timezone.utc).replace(tzinfo=None) - created).total_seconds() / 86400) if created else 0
    return {
        "id": row.id, "revision": policy.revision, "operator": row.operator,
        "created_at": created.isoformat() if created else None,
        "config_value": policy.as_dict(), "recoverable": age_days <= retention_days,
        "retention_days": retention_days,
    }


def list_visibility_policy_history(db: Session) -> list[dict]:
    retention = _audit_retention_days(db)
    rows = db.query(AuditLog).filter(
        AuditLog.target_type == "system",
        AuditLog.target_id == VISIBILITY_POLICY_KEY,
        AuditLog.action == "manual_edit",
    ).order_by(AuditLog.created_at.desc()).all()
    return [entry for row in rows if (entry := _history_entry(row, retention))]


def get_visibility_policy_history(db: Session, revision: int) -> dict:
    for entry in list_visibility_policy_history(db):
        if entry["revision"] == revision:
            return entry
    raise BusinessException(40401, f"策略 revision={revision} 不存在或不可恢复")


def restore_visibility_policy(
    db: Session, revision: int, expected_revision: int, operator: str,
    confirm_sensitive_expansion: bool = False,
) -> dict:
    entry = get_visibility_policy_history(db, revision)
    if not entry["recoverable"]:
        raise BusinessException(40904, "该策略版本已超过审计保留期限，不能恢复")
    return _save_visibility_policy(
        db, entry["config_value"], expected_revision, operator,
        confirm_sensitive_expansion, reason=f"visibility_policy_restore:{revision}",
    )


def check_visibility_policy_integrity(db: Session) -> dict:
    """Verify that the active database policy has a complete successful audit."""

    item = _policy_item(db)
    current = _policy_document(item)
    rows = db.query(AuditLog).filter(
        AuditLog.target_type == "system",
        AuditLog.target_id == VISIBILITY_POLICY_KEY,
        AuditLog.action == "manual_edit",
    ).order_by(AuditLog.created_at.desc()).limit(200).all()
    retention = _audit_retention_days(db)
    for row in rows:
        entry = _history_entry(row, retention)
        if (
            entry
            and entry["revision"] == current.revision
            and entry["config_value"] == current.as_dict()
        ):
            return {"ok": True, "revision": current.revision, "audit_id": row.id}
    return {
        "ok": False, "revision": current.revision,
        "error": "active_revision_success_audit_missing",
    }
