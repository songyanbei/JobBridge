"""岗位管理路由（Phase 5 模块 E）。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.core.csv_export import rows_to_csv_bytes
from app.core.responses import ok, paged
from app.models import AdminUser
from app.schemas.job import JobRead
from app.services import job_admin_service

router = APIRouter(prefix="/admin/jobs", tags=["admin-jobs"])


class JobEditRequest(BaseModel):
    version: int = Field(..., ge=1)
    fields: dict[str, Any] = Field(default_factory=dict)


class DelistRequest(BaseModel):
    reason: str = Field(..., description="manual_delist | filled")


class ExtendRequest(BaseModel):
    days: int = Field(..., description="15 或 30")


def _collect_filters(
    city: str | None, district: str | None, job_category: str | None,
    pay_type: str | None, audit_status: str | None, delist_reason: str | None,
    owner_userid: str | None, created_from: datetime | None, created_to: datetime | None,
    expires_from: datetime | None, expires_to: datetime | None,
    salary_min: int | None, salary_max: int | None,
) -> dict:
    return {
        "city": city, "district": district, "job_category": job_category,
        "pay_type": pay_type, "audit_status": audit_status,
        "delist_reason": delist_reason, "owner_userid": owner_userid,
        "created_from": created_from, "created_to": created_to,
        "expires_from": expires_from, "expires_to": expires_to,
        "salary_min": salary_min, "salary_max": salary_max,
    }


@router.get("", summary="岗位列表（admin）")
def list_jobs(
    city: str | None = None,
    district: str | None = None,
    job_category: str | None = None,
    pay_type: str | None = None,
    audit_status: str | None = None,
    delist_reason: str | None = None,
    owner_userid: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    expires_from: datetime | None = None,
    expires_to: datetime | None = None,
    salary_min: int | None = None,
    salary_max: int | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    sort: str = "created_at:desc",
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    filters = _collect_filters(
        city, district, job_category, pay_type, audit_status, delist_reason,
        owner_userid, created_from, created_to, expires_from, expires_to,
        salary_min, salary_max,
    )
    rows, total = job_admin_service.list_jobs(db, filters, page, size, sort)
    return paged([JobRead.model_validate(r).model_dump(mode="json") for r in rows], total, page, size)


@router.get("/export", summary="岗位导出 CSV")
def export_jobs(
    city: str | None = None,
    district: str | None = None,
    job_category: str | None = None,
    pay_type: str | None = None,
    audit_status: str | None = None,
    delist_reason: str | None = None,
    owner_userid: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    expires_from: datetime | None = None,
    expires_to: datetime | None = None,
    salary_min: int | None = None,
    salary_max: int | None = None,
    sort: str = "created_at:desc",
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    filters = _collect_filters(
        city, district, job_category, pay_type, audit_status, delist_reason,
        owner_userid, created_from, created_to, expires_from, expires_to,
        salary_min, salary_max,
    )
    rows = job_admin_service.export_rows(db, filters, sort)
    headers = [
        "id", "owner_userid", "city", "district", "job_category",
        "salary_floor_monthly", "salary_ceiling_monthly", "pay_type",
        "headcount", "gender_required", "age_min", "age_max", "is_long_term",
        "audit_status", "audit_reason", "delist_reason",
        "created_at", "expires_at", "version",
    ]
    body = []
    for r in rows:
        body.append([
            r.id, r.owner_userid, r.city, r.district, r.job_category,
            r.salary_floor_monthly, r.salary_ceiling_monthly, r.pay_type,
            r.headcount, r.gender_required, r.age_min, r.age_max, r.is_long_term,
            r.audit_status, r.audit_reason, r.delist_reason,
            r.created_at, r.expires_at, r.version,
        ])
    data = rows_to_csv_bytes(headers, body)
    filename = f"jobs_{datetime.now().strftime('%Y%m%d%H%M')}.csv"
    return Response(content=data, media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
    })


@router.get("/{job_id}", summary="岗位详情（admin）")
def get_job(
    job_id: int,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    job = job_admin_service.get_job(db, job_id)
    return ok(JobRead.model_validate(job).model_dump(mode="json"))


@router.put("/{job_id}", summary="岗位编辑（带 version 乐观锁）")
def update_job(
    job_id: int,
    req: JobEditRequest,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    job = job_admin_service.update_job(db, job_id, req.version, req.fields, current.username)
    return ok(JobRead.model_validate(job).model_dump(mode="json"))


@router.post("/{job_id}/delist", summary="岗位下架")
def delist_job(
    job_id: int,
    req: DelistRequest,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    job_admin_service.delist(db, job_id, req.reason, current.username)
    return ok()


@router.post("/{job_id}/extend", summary="岗位延期")
def extend_job(
    job_id: int,
    req: ExtendRequest,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    job = job_admin_service.extend(db, job_id, req.days, current.username)
    return ok({"expires_at": job.expires_at.isoformat() if job.expires_at else None})


@router.post("/{job_id}/restore", summary="岗位取消下架")
def restore_job(
    job_id: int,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    job_admin_service.restore(db, job_id, current.username)
    return ok()
