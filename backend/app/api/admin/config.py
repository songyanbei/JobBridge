"""系统配置路由（Phase 5 模块 G）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin_password_changed as require_admin
from app.core.responses import ok
from app.models import AdminUser
from app.schemas.admin import SystemConfigUpdate
from app.services import system_config_service

router = APIRouter(prefix="/admin/config", tags=["admin-config"])


@router.get("", summary="系统配置（按 key 前缀分组）")
def list_config(
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    return ok(system_config_service.list_grouped(db))


@router.put("/{key}", summary="更新单项系统配置")
def update_config(
    key: str,
    req: SystemConfigUpdate,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    result = system_config_service.update(
        db, key, req.config_value, req.value_type, current.username,
    )
    return ok(result)
