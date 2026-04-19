"""审核工作台路由（Phase 5 模块 C）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin_password_changed as require_admin
from app.core.responses import ok, paged
from app.models import AdminUser
from app.schemas.audit import (
    EditRequest,
    PassRequest,
    RejectRequest,
)
from app.services import audit_workbench_service as svc

router = APIRouter(prefix="/admin/audit", tags=["admin-audit"])


def _serialize_queue_item(item) -> dict:
    obj = item.obj
    raw = (obj.raw_text or "")[:120]
    return {
        "id": obj.id,
        "target_type": item.target_type,
        "owner_userid": obj.owner_userid,
        "audit_status": obj.audit_status,
        "risk_level": item.risk_level,
        "extracted_brief": raw,
        "locked_by": item.locked_by,
        "created_at": obj.created_at.isoformat() if obj.created_at else None,
        "version": int(obj.version or 0),
    }


@router.get("/queue", summary="待审队列")
def queue(
    status: str = Query("pending"),
    target_type: str | None = Query(None, description="job / resume"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    items, total = svc.list_queue(db, status=status, target_type=target_type, page=page, size=size)
    return paged([_serialize_queue_item(x) for x in items], total, page, size)


@router.get("/pending-count", summary="待审数量")
def pending_count(db: Session = Depends(get_db), _: AdminUser = Depends(require_admin)):
    return ok(svc.get_pending_count(db))


@router.get("/{target_type}/{target_id}", summary="审核详情")
def detail(
    target_type: str,
    target_id: int,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    return ok(svc.get_detail(db, target_type, target_id))


@router.post("/{target_type}/{target_id}/lock", summary="审核软锁")
def lock(
    target_type: str,
    target_id: int,
    current: AdminUser = Depends(require_admin),
):
    svc.lock(target_type, target_id, current.username)
    return ok()


@router.post("/{target_type}/{target_id}/unlock", summary="释放审核软锁")
def unlock(
    target_type: str,
    target_id: int,
    current: AdminUser = Depends(require_admin),
):
    released = svc.unlock(target_type, target_id, current.username)
    return ok({"released": released})


@router.post("/{target_type}/{target_id}/pass", summary="审核通过")
def pass_item(
    target_type: str,
    target_id: int,
    req: PassRequest,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    svc.pass_action(db, target_type, target_id, req.version, current.username)
    return ok()


@router.post("/{target_type}/{target_id}/reject", summary="审核驳回")
def reject_item(
    target_type: str,
    target_id: int,
    req: RejectRequest,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    svc.reject_action(
        db, target_type, target_id,
        version=req.version, reason=req.reason,
        operator=current.username,
        notify=req.notify, block_user=req.block_user,
    )
    return ok()


@router.put("/{target_type}/{target_id}/edit", summary="审核修正字段")
def edit_item(
    target_type: str,
    target_id: int,
    req: EditRequest,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    svc.edit_action(db, target_type, target_id, req.version, req.fields, current.username)
    return ok()


@router.post("/{target_type}/{target_id}/undo", summary="撤销最近一次审核动作（30s 内）")
def undo_item(
    target_type: str,
    target_id: int,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    svc.undo(db, target_type, target_id, current.username)
    return ok()
