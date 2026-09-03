"""独立演示工作区管理 API。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin_role
from app.core.exceptions import BusinessException
from app.core.responses import ok
from app.models import AdminUser
from app.services import demo_mode_service, demo_workspace_admin_service as service

router = APIRouter(prefix="/admin/demo", tags=["admin-demo"])
_SUPER = ("super_admin",)


class DemoReasonRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=255)
    expected_version: int | None = Field(default=None, ge=1)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError("reason must be a non-empty printable string")
        return value


class DemoCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    bot_id: str = Field(..., min_length=1, max_length=128)
    actor_digest: str = Field(..., min_length=64, max_length=64)
    canonical_actor_userid: str | None = Field(default=None, max_length=64)
    demo_id: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("name", "bot_id", "actor_digest")
    @classmethod
    def strip_values(cls, value: str) -> str:
        return value.strip()


def _translate(exc: Exception) -> None:
    if isinstance(exc, service.DemoAdminNotFound):
        raise BusinessException(40401, str(exc)) from exc
    if isinstance(exc, service.DemoAdminConflict):
        raise BusinessException(40902, str(exc)) from exc
    if isinstance(exc, (service.DemoAdminError, demo_mode_service.DemoModeError)):
        raise BusinessException(40301, str(exc)) from exc
    raise exc


@router.get("/workspaces", summary="查询演示工作区")
def list_demo_workspaces(
    status: str | None = Query(default=None), limit: int = Query(default=100, ge=1, le=200),
    db: Session = Depends(get_db), _: AdminUser = Depends(require_admin_role(*_SUPER)),
):
    try:
        return ok(service.list_workspaces(db, status=status, limit=limit))
    except Exception as exc:
        _translate(exc)


@router.post("/workspaces", summary="创建演示工作区")
def create_demo_workspace(
    req: DemoCreateRequest, db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin_role(*_SUPER)),
):
    try:
        row = demo_mode_service.create_workspace(
            db, name=req.name, bot_id=req.bot_id, actor_digest_value=req.actor_digest,
            canonical_actor_userid=req.canonical_actor_userid, demo_id=req.demo_id,
            created_by=current.username,
        )
        db.commit()
        return ok(service.workspace_status(db, demo_id=row.demo_id))
    except Exception as exc:
        db.rollback()
        _translate(exc)


@router.get("/{demo_id}", summary="读取演示工作区状态")
def get_demo_workspace(
    demo_id: str, db: Session = Depends(get_db), _: AdminUser = Depends(require_admin_role(*_SUPER)),
):
    try:
        return ok(service.workspace_status(db, demo_id=demo_id))
    except Exception as exc:
        _translate(exc)


@router.get("/{demo_id}/status", summary="读取演示清理状态")
def get_demo_workspace_status(
    demo_id: str, db: Session = Depends(get_db), _: AdminUser = Depends(require_admin_role(*_SUPER)),
):
    try:
        return ok(service.workspace_status(db, demo_id=demo_id))
    except Exception as exc:
        _translate(exc)


@router.get("/{demo_id}/preview", summary="预览演示工作区清理范围")
@router.post("/{demo_id}/preview", summary="预览演示工作区清理范围")
def preview_demo_workspace(
    demo_id: str, db: Session = Depends(get_db), _: AdminUser = Depends(require_admin_role(*_SUPER)),
):
    try:
        return ok(service.preview_workspace(db, demo_id=demo_id))
    except Exception as exc:
        _translate(exc)


@router.post("/{demo_id}/disable", summary="禁用并下架演示工作区")
def disable_demo_workspace(
    demo_id: str, req: DemoReasonRequest, db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin_role(*_SUPER)),
):
    try:
        return ok(service.disable_workspace(
            db, demo_id=demo_id, reason=req.reason, operator=current.username,
            expected_version=req.expected_version,
        ))
    except Exception as exc:
        _translate(exc)


@router.post("/{demo_id}/cleanup", summary="清理演示工作区")
def cleanup_demo_workspace(
    demo_id: str, req: DemoReasonRequest, db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin_role(*_SUPER)),
):
    try:
        return ok(service.cleanup_workspace(
            db, demo_id=demo_id, reason=req.reason, operator=current.username,
            expected_version=req.expected_version,
        ))
    except Exception as exc:
        _translate(exc)


@router.post("/{demo_id}/retry", summary="重试演示工作区清理")
@router.post("/{demo_id}/cleanup/retry", summary="重试演示工作区清理")
def retry_demo_workspace_cleanup(
    demo_id: str, req: DemoReasonRequest, db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin_role(*_SUPER)),
):
    try:
        return ok(service.retry_cleanup(
            db, demo_id=demo_id, reason=req.reason, operator=current.username,
            expected_version=req.expected_version,
        ))
    except Exception as exc:
        _translate(exc)

