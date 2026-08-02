"""Database-backed recommendation field visibility policy.

The loader deliberately performs one primary-database read per call and keeps no
Redis or process-local policy cache.  Callers must load once at the request
boundary and pass the immutable snapshot through the rest of the pipeline.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import SystemConfig
from app.services.visibility_contract import (
    BUILTIN_SAFE_POLICY,
    BUILTIN_SAFE_POLICY_ID,
    BUSINESS_DEFAULT_POLICY,
    WORKER_JOB_FIELDS,
    ViewerRole,
    VisibilityScene,
    hard_visibility_limit,
    registry_for,
)
from app.tasks.common import log_event

VISIBILITY_POLICY_KEY = "visibility.recommendation_fields"
SUPPORTED_SCHEMA_VERSION = 1
PRIMARY_READ_EXECUTION_OPTION = "visibility_policy_primary_read"

PolicySource = Literal["database", "builtin_safe_fallback"]
POLICY_LOAD_ALERT_THRESHOLD = 3
POLICY_LOAD_ALERT_DEDUPE_SECONDS = 300
_metrics_lock = threading.Lock()
_load_failure_total = 0
_consecutive_load_failures = 0
_last_alert_by_reason: dict[str, float] = {}


def visibility_policy_load_metrics() -> dict:
    with _metrics_lock:
        return {
            "visibility_policy_load_failure_total": _load_failure_total,
            "consecutive_failures": _consecutive_load_failures,
            "alert_threshold": POLICY_LOAD_ALERT_THRESHOLD,
            "alert_dedupe_seconds": POLICY_LOAD_ALERT_DEDUPE_SECONDS,
        }


def _record_policy_load_success() -> None:
    global _consecutive_load_failures
    with _metrics_lock:
        _consecutive_load_failures = 0


def _record_policy_load_failure(reason: str) -> None:
    global _load_failure_total, _consecutive_load_failures
    now = time.monotonic()
    should_alert = False
    with _metrics_lock:
        _load_failure_total += 1
        _consecutive_load_failures += 1
        total = _load_failure_total
        consecutive = _consecutive_load_failures
        last_alert = _last_alert_by_reason.get(reason, 0.0)
        if consecutive >= POLICY_LOAD_ALERT_THRESHOLD and now - last_alert >= POLICY_LOAD_ALERT_DEDUPE_SECONDS:
            _last_alert_by_reason[reason] = now
            should_alert = True
    log_event(
        "visibility_policy_load_failure_metric",
        metric="visibility_policy_load_failure_total", total=total,
        consecutive_failures=consecutive, failure_reason=reason,
    )
    if should_alert:
        log_event(
            "visibility_policy_load_alert", severity="alert",
            consecutive_failures=consecutive, failure_reason=reason,
            dedupe_seconds=POLICY_LOAD_ALERT_DEDUPE_SECONDS,
        )


class VisibilityPolicyValidationError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class NormalizedPolicy:
    schema_version: int
    revision: int
    matrix: Mapping[VisibilityScene, Mapping[ViewerRole, tuple[str, ...]]]

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            **{
                scene.value: {
                    role.value: list(self.matrix[scene][role])
                    for role in ViewerRole
                }
                for scene in VisibilityScene
            },
        }


@dataclass(frozen=True, slots=True)
class EffectivePolicySnapshot:
    scene: VisibilityScene
    role: str
    policy_source: PolicySource
    policy_revision: int | None
    fallback_policy_id: str | None
    visible_fields: tuple[str, ...]
    reranker_fields: tuple[str, ...]
    soft_preference_fields: tuple[str, ...]


def default_policy_document(revision: int = 1) -> dict:
    """Return a detached JSON-compatible business-default policy document."""

    return {
        "schema_version": SUPPORTED_SCHEMA_VERSION,
        "revision": revision,
        **{
            scene.value: {
                role.value: list(BUSINESS_DEFAULT_POLICY[scene][role])
                for role in ViewerRole
            }
            for scene in VisibilityScene
        },
    }


def normalize_policy(raw: str | Mapping) -> NormalizedPolicy:
    """Parse, semantically validate and registry-order a complete policy."""

    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise VisibilityPolicyValidationError(
                "invalid_json", "配置不是合法 JSON",
            ) from exc
    elif isinstance(raw, Mapping):
        payload = dict(raw)
    else:
        raise VisibilityPolicyValidationError("invalid_type", "配置必须是 JSON 对象")

    if not isinstance(payload, dict):
        raise VisibilityPolicyValidationError("invalid_type", "配置必须是 JSON 对象")

    allowed_top_keys = {
        "schema_version", "revision", *(scene.value for scene in VisibilityScene),
    }
    unknown_top_keys = set(payload) - allowed_top_keys
    if unknown_top_keys:
        raise VisibilityPolicyValidationError(
            "unknown_scene", f"存在未知场景: {sorted(unknown_top_keys)}",
        )
    missing_top_keys = allowed_top_keys - set(payload)
    if missing_top_keys:
        raise VisibilityPolicyValidationError(
            "missing_section", f"缺少配置段: {sorted(missing_top_keys)}",
        )

    schema_version = payload.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != SUPPORTED_SCHEMA_VERSION
    ):
        raise VisibilityPolicyValidationError(
            "unsupported_schema_version", "仅支持 schema_version=1",
        )
    revision = payload.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise VisibilityPolicyValidationError(
            "invalid_revision", "revision 必须是正整数",
        )

    normalized_matrix: dict[
        VisibilityScene, Mapping[ViewerRole, tuple[str, ...]]
    ] = {}
    expected_roles = {role.value for role in ViewerRole}
    for scene in VisibilityScene:
        role_payload = payload[scene.value]
        if not isinstance(role_payload, dict):
            raise VisibilityPolicyValidationError(
                "invalid_scene", f"{scene.value} 必须是角色对象",
            )
        unknown_roles = set(role_payload) - expected_roles
        missing_roles = expected_roles - set(role_payload)
        if unknown_roles:
            raise VisibilityPolicyValidationError(
                "unknown_role", f"{scene.value} 存在未知角色: {sorted(unknown_roles)}",
            )
        if missing_roles:
            raise VisibilityPolicyValidationError(
                "missing_role", f"{scene.value} 缺少角色: {sorted(missing_roles)}",
            )

        normalized_roles: dict[ViewerRole, tuple[str, ...]] = {}
        registry_order = tuple(registry_for(scene))
        for role in ViewerRole:
            fields = role_payload[role.value]
            if not isinstance(fields, list) or any(
                not isinstance(field, str) for field in fields
            ):
                raise VisibilityPolicyValidationError(
                    "invalid_fields", f"{scene.value}.{role.value} 必须是字符串数组",
                )
            if len(fields) != len(set(fields)):
                raise VisibilityPolicyValidationError(
                    "duplicate_field", f"{scene.value}.{role.value} 包含重复字段",
                )
            unknown_fields = set(fields) - set(registry_order)
            if unknown_fields:
                raise VisibilityPolicyValidationError(
                    "unknown_field",
                    f"{scene.value}.{role.value} 包含未知字段: {sorted(unknown_fields)}",
                )
            hard_limit = set(hard_visibility_limit(scene, role))
            forbidden_fields = set(fields) - hard_limit
            if forbidden_fields:
                raise VisibilityPolicyValidationError(
                    "field_exceeds_hard_limit",
                    f"{scene.value}.{role.value} 超出安全上限: {sorted(forbidden_fields)}",
                )
            if scene is VisibilityScene.JOB_SEARCH and role is ViewerRole.WORKER:
                if tuple(fields) != WORKER_JOB_FIELDS:
                    raise VisibilityPolicyValidationError(
                        "worker_job_fields_fixed",
                        "worker.job_search 必须严格为招聘工厂、岗位、薪资三字段",
                    )
            normalized_roles[role] = tuple(
                field for field in registry_order if field in fields
            )
        normalized_matrix[scene] = MappingProxyType(normalized_roles)

    return NormalizedPolicy(
        schema_version=schema_version,
        revision=revision,
        matrix=MappingProxyType(normalized_matrix),
    )


def _snapshot_from_fields(
    *,
    scene: VisibilityScene,
    role: str,
    fields: tuple[str, ...],
    source: PolicySource,
    revision: int | None,
    fallback_policy_id: str | None,
) -> EffectivePolicySnapshot:
    registry = registry_for(scene)
    reranker_fields: list[str] = ["id"]
    soft_preference_fields: list[str] = []
    for field_name in fields:
        spec = registry[field_name]
        for source_field in spec.ranking_projection:
            if source_field not in reranker_fields:
                reranker_fields.append(source_field)
        for preference_field in spec.soft_preference_mapping:
            if preference_field not in soft_preference_fields:
                soft_preference_fields.append(preference_field)
    return EffectivePolicySnapshot(
        scene=scene,
        role=role,
        policy_source=source,
        policy_revision=revision,
        fallback_policy_id=fallback_policy_id,
        visible_fields=fields,
        reranker_fields=tuple(reranker_fields),
        soft_preference_fields=tuple(soft_preference_fields),
    )


def builtin_safe_snapshot(
    scene: VisibilityScene | str,
    role: ViewerRole | str,
) -> EffectivePolicySnapshot:
    scene_value = VisibilityScene(scene)
    role_text = str(role)
    try:
        role_value = ViewerRole(role_text)
    except ValueError:
        fields: tuple[str, ...] = ()
    else:
        fields = BUILTIN_SAFE_POLICY[scene_value][role_value]
    return _snapshot_from_fields(
        scene=scene_value,
        role=role_text,
        fields=fields,
        source="builtin_safe_fallback",
        revision=None,
        fallback_policy_id=BUILTIN_SAFE_POLICY_ID,
    )


def snapshot_from_policy(
    policy: NormalizedPolicy,
    scene: VisibilityScene | str,
    role: ViewerRole | str,
) -> EffectivePolicySnapshot:
    scene_value = VisibilityScene(scene)
    role_text = str(role)
    try:
        role_value = ViewerRole(role_text)
    except ValueError:
        fields: tuple[str, ...] = ()
    else:
        configured_fields = policy.matrix[scene_value][role_value]
        hard_limit = set(hard_visibility_limit(scene_value, role_value))
        fields = tuple(
            field for field in registry_for(scene_value)
            if field in configured_fields and field in hard_limit
        )
    return _snapshot_from_fields(
        scene=scene_value,
        role=role_text,
        fields=fields,
        source="database",
        revision=policy.revision,
        fallback_policy_id=None,
    )


def load(
    db: Session,
    scene: VisibilityScene | str,
    role: ViewerRole | str,
) -> EffectivePolicySnapshot:
    """Read one policy row from the primary DB and return an immutable snapshot."""

    scene_value = VisibilityScene(scene)
    loaded_revision: int | None = None
    try:
        statement = (
            sa.select(SystemConfig)
            .where(SystemConfig.config_key == VISIBILITY_POLICY_KEY)
            .execution_options(**{PRIMARY_READ_EXECUTION_OPTION: True})
        )
        row = db.execute(statement).scalar_one_or_none()
        if row is None:
            raise VisibilityPolicyValidationError(
                "missing_config", "可见性策略配置行不存在",
            )
        if getattr(row, "value_type", None) != "json":
            raise VisibilityPolicyValidationError(
                "invalid_value_type", "可见性策略必须使用 json 类型",
            )
        policy = normalize_policy(row.config_value)
        loaded_revision = policy.revision
        _record_policy_load_success()
        return snapshot_from_policy(policy, scene_value, role)
    except Exception as exc:
        reason = exc.code if isinstance(exc, VisibilityPolicyValidationError) else type(exc).__name__
        log_event(
            "visibility_policy_load_failed",
            config_key=VISIBILITY_POLICY_KEY,
            loaded_revision=loaded_revision,
            failure_reason=reason,
            policy_source="builtin_safe_fallback",
            fallback_policy_id=BUILTIN_SAFE_POLICY_ID,
        )
        _record_policy_load_failure(reason)
        return builtin_safe_snapshot(scene_value, role)


def project_for_reranker(
    scene: VisibilityScene | str,
    role: ViewerRole | str,
    effective_policy: EffectivePolicySnapshot,
    candidate: Mapping,
) -> dict:
    """Project only ID plus visible, hard-allowed and ranking-approved raw fields."""

    scene_value = VisibilityScene(scene)
    role_text = str(role)
    if effective_policy.scene is not scene_value or effective_policy.role != role_text:
        return {"id": candidate.get("id")}
    try:
        role_value = ViewerRole(role_text)
    except ValueError:
        return {"id": candidate.get("id")}
    hard_limit = set(hard_visibility_limit(scene_value, role_value))
    visible_fields = tuple(
        field
        for field in registry_for(scene_value)
        if field in effective_policy.visible_fields and field in hard_limit
    )
    approved_projection = ["id"]
    for field in visible_fields:
        for source_field in registry_for(scene_value)[field].ranking_projection:
            if source_field not in approved_projection:
                approved_projection.append(source_field)
    return {
        key: candidate.get(key)
        for key in approved_projection
        if key == "id" or key in candidate
    }


def project_soft_preferences(
    effective_policy: EffectivePolicySnapshot,
    soft_preferences: Mapping | None,
) -> dict:
    """Drop preference dimensions hidden from or non-rankable for this viewer."""

    preferences = soft_preferences or {}
    try:
        role = ViewerRole(effective_policy.role)
    except ValueError:
        return {}
    hard_limit = set(hard_visibility_limit(effective_policy.scene, role))
    approved_fields: list[str] = []
    registry = registry_for(effective_policy.scene)
    for field in effective_policy.visible_fields:
        if field not in hard_limit or field not in registry:
            continue
        for preference_field in registry[field].soft_preference_mapping:
            if preference_field not in approved_fields:
                approved_fields.append(preference_field)
    return {
        field: preferences[field]
        for field in approved_fields
        if field in preferences
    }


def project_for_safe_log(candidate: Mapping) -> dict:
    """Candidate projection allowed in exception/debug context."""

    return {"id": candidate.get("id")}
