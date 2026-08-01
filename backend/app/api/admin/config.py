"""系统配置路由（Phase 5 模块 G）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin_password_changed as require_admin
from app.core.responses import ok
from app.core.exceptions import BusinessException
from app.models import AdminUser
from app.schemas.admin import SystemConfigUpdate
from app.services import system_config_service

router = APIRouter(prefix="/admin/config", tags=["admin-config"])


class VisibilityPolicyRequest(BaseModel):
    policy: dict = Field(..., description="完整策略 JSON 对象")
    expected_revision: int = Field(..., ge=1)
    confirm_sensitive_expansion: bool = False


class VisibilityPolicyValidateRequest(BaseModel):
    policy: dict


class VisibilityPolicyRestoreRequest(BaseModel):
    expected_revision: int = Field(..., ge=1)
    confirm_sensitive_expansion: bool = False


@router.get("", summary="系统配置（按 key 前缀分组）")
def list_config(
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    return ok(system_config_service.list_grouped(db))


@router.get("/visibility-policy", summary="读取推荐权限字段策略")
def get_visibility_policy(
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    return ok(system_config_service.get_visibility_policy(db))


@router.post("/visibility-policy/validate", summary="校验推荐权限字段策略")
def validate_visibility_policy(
    req: VisibilityPolicyValidateRequest,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    return ok(system_config_service.validate_visibility_policy(db, req.policy))


@router.put("/visibility-policy", summary="保存推荐权限字段策略")
def save_visibility_policy(
    req: VisibilityPolicyRequest,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    return ok(system_config_service.update_visibility_policy(
        db, req.policy, req.expected_revision, current.username,
        req.confirm_sensitive_expansion,
    ))


@router.get("/visibility-policy/history", summary="推荐权限字段策略历史")
def visibility_policy_history(
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    return ok(system_config_service.list_visibility_policy_history(db))


@router.get("/visibility-policy/history/{revision}", summary="查看推荐权限字段策略历史版本")
def visibility_policy_history_detail(
    revision: int,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    return ok(system_config_service.get_visibility_policy_history(db, revision))


@router.post("/visibility-policy/history/{revision}/restore", summary="恢复推荐权限字段策略")
def restore_visibility_policy(
    revision: int,
    req: VisibilityPolicyRestoreRequest,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    return ok(system_config_service.restore_visibility_policy(
        db, revision, req.expected_revision, current.username,
        req.confirm_sensitive_expansion,
    ))


# Keep this dynamic route last so it cannot shadow dedicated policy endpoints.
@router.put("/{key}", summary="更新单项系统配置")
def update_config(
    key: str,
    req: SystemConfigUpdate,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    try:
        result = system_config_service.update(
            db, key, req.config_value, req.value_type, current.username,
        )
    except ValueError as exc:
        if str(exc) == "config_locked_by_recommendation_v1":
            raise BusinessException(40905, str(exc)) from exc
        raise
    return ok(result)
