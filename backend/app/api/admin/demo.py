"""独立演示工作区管理 API。"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin_role
from app.core.exceptions import BusinessException
from app.core.responses import ok
from app.models import AdminUser, DemoWorkspaceMember
from app.services.admin_log_service import write_admin_log
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


class DemoMemberRequest(BaseModel):
    bot_id: str = Field(..., min_length=1, max_length=128)
    actor_digest: str = Field(..., min_length=64, max_length=64)
    canonical_actor_userid: str | None = Field(default=None, max_length=64)
    expires_at: datetime | None = None

    @field_validator("bot_id", "actor_digest")
    @classmethod
    def strip_member_values(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value

    @field_validator("actor_digest")
    @classmethod
    def validate_actor_digest(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
            raise ValueError("actor_digest must be a 64-character hex digest")
        return value.lower()

    @field_validator("expires_at")
    @classmethod
    def normalize_expiry(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value


class DemoMemberRevokeRequest(BaseModel):
    reason: str = Field(default="operator_revoked", min_length=1, max_length=255)

    @field_validator("reason")
    @classmethod
    def validate_revoke_reason(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError("reason must be a non-empty printable string")
        return value


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


@router.post("/{demo_id}/members", summary="授权企微账号进入演示工作区")
def grant_demo_member(
    demo_id: str,
    req: DemoMemberRequest,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin_role(*_SUPER)),
):
    try:
        existing = db.query(DemoWorkspaceMember).filter(
            DemoWorkspaceMember.demo_id == demo_id,
            DemoWorkspaceMember.bot_id == req.bot_id,
            DemoWorkspaceMember.opaque_actor_digest == req.actor_digest,
        ).first()
        before = None if existing is None else {
            "membership_status": existing.membership_status,
            "expires_at": existing.expires_at,
        }
        member = demo_mode_service.authorize_member(
            db, demo_id=demo_id, bot_id=req.bot_id,
            actor_digest_value=req.actor_digest, granted_by=current.username,
            canonical_actor_userid=req.canonical_actor_userid,
            expires_at=req.expires_at,
        )
        write_admin_log(
            db, target_type="system", target_id=demo_id, action="manual_edit",
            operator=current.username,
            before=before,
            after={"membership_status": member.membership_status, "expires_at": member.expires_at},
            reason=f"demo_member_grant:actor_digest={req.actor_digest}",
        )
        db.commit()
        return ok({
            "demo_id": demo_id,
            "member_id": member.member_id,
            "bot_id": member.bot_id,
            "actor_digest": member.opaque_actor_digest,
            "status": member.membership_status,
            "expires_at": member.expires_at,
        })
    except Exception as exc:
        db.rollback()
        _translate(exc)


def _revoke_demo_member(
    demo_id: str,
    actor_digest: str,
    db: Session,
    current: AdminUser,
    reason: str,
):
    if not re.fullmatch(r"[0-9a-fA-F]{64}", actor_digest or ""):
        raise BusinessException(40101, "actor_digest must be a 64-character hex digest")
    actor_digest = actor_digest.lower()
    try:
        # Read the existing row only for a privacy-safe before snapshot; the
        # digest is opaque and no plaintext actor identifier is ever logged.
        existing = db.query(DemoWorkspaceMember).filter(
            DemoWorkspaceMember.demo_id == demo_id,
            DemoWorkspaceMember.opaque_actor_digest == actor_digest,
        ).first()
        before = None if existing is None else {
            "membership_status": existing.membership_status,
            "expires_at": existing.expires_at,
        }
        bot_id = existing.bot_id if existing is not None else ""
        if bot_id:
            demo_mode_service.revoke_member(
                db, demo_id=demo_id, bot_id=bot_id,
                actor_digest_value=actor_digest,
            )
        else:
            # Still invoke the control-plane gate and workspace lookup for a
            # deterministic not-found/disabled response.
            service.workspace_status(db, demo_id=demo_id)
        after = None if existing is None else {
            "membership_status": existing.membership_status,
            "expires_at": existing.expires_at,
        }
        write_admin_log(
            db, target_type="system", target_id=demo_id, action="manual_edit",
            operator=current.username, before=before, after=after,
            reason=f"demo_member_revoke:actor_digest={actor_digest};{reason[:180]}",
        )
        db.commit()
        return ok({
            "demo_id": demo_id,
            "actor_digest": actor_digest,
            "status": "revoked" if existing is not None else "absent",
        })
    except Exception as exc:
        db.rollback()
        _translate(exc)


@router.post("/{demo_id}/members/{actor_digest}", summary="撤销企微账号演示权限")
def revoke_demo_member_post(
    demo_id: str,
    actor_digest: str,
    req: DemoMemberRevokeRequest | None = None,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin_role(*_SUPER)),
):
    return _revoke_demo_member(demo_id, actor_digest, db, current, req.reason if req else "operator_revoked")


@router.delete("/{demo_id}/members/{actor_digest}", summary="撤销企微账号演示权限")
def revoke_demo_member_delete(
    demo_id: str,
    actor_digest: str,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin_role(*_SUPER)),
):
    return _revoke_demo_member(demo_id, actor_digest, db, current, "operator_revoked")


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
