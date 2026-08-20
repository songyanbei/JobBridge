"""Phase-11 cleanup operations console API."""
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin_role
from app.core.responses import ok
from app.models import AdminUser
from app.services import cleanup_admin_service

router = APIRouter(prefix="/admin/cleanup", tags=["admin-cleanup"])
_OPERATOR = ("operator", "super_admin")
_SUPER = ("super_admin",)


class RedriveRequest(BaseModel):
    kind: str
    ids: list[int] = Field(..., min_length=1, max_length=50)
    reason: str = Field(..., min_length=1, max_length=160)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason must not be blank")
        if any(ord(char) < 32 for char in value):
            raise ValueError("reason must not contain control characters")
        return value


class MediaApprovalRequest(BaseModel):
    disposition: str
    reason: str = Field(..., min_length=1, max_length=255)


@router.get("/tasks", summary="查询目标清理任务")
def target_tasks(
    status: str | None = None, limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin_role(*_OPERATOR)),
):
    return ok(cleanup_admin_service.list_target_tasks(db, status=status, limit=limit))


@router.get("/media-isolation", summary="查询媒体隔离问题")
def media_issues(
    status: str | None = None, limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin_role(*_OPERATOR)),
):
    return ok(cleanup_admin_service.list_media_issues(db, status=status, limit=limit))


@router.get("/media-dead-letters", summary="查询媒体清理 dead letter")
def media_dead_letters(
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin_role(*_OPERATOR)),
):
    return ok(cleanup_admin_service.list_media_dead_letters(db, limit=limit))


@router.post("/dead-letters/retry", summary="重驱清理 dead letter")
def retry_dead_letters(
    req: RedriveRequest, db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin_role(*_SUPER)),
):
    return ok(cleanup_admin_service.redrive_dead_letters(
        db, kind=req.kind, ids=req.ids, reason=req.reason, operator=current.username,
    ))


@router.post("/media-isolation/{issue_id}/approve", summary="审批媒体隔离处置")
def approve_media_issue(
    issue_id: int, req: MediaApprovalRequest, db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin_role(*_SUPER)),
):
    return ok(cleanup_admin_service.approve_media_issue(
        db, issue_id=issue_id, disposition=req.disposition, reason=req.reason,
        operator=current.username,
    ))


@router.post("/media-isolation/{issue_id}/execute", summary="执行媒体隔离处置")
def execute_media_issue(
    issue_id: int, db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin_role(*_SUPER)),
):
    return ok(cleanup_admin_service.execute_media_issue(
        db, issue_id=issue_id, operator=current.username,
    ))
