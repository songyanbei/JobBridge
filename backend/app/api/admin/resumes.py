"""简历管理路由（Phase 5 模块 E）。"""
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
from app.schemas.resume import ResumeRead
from app.services import resume_admin_service

router = APIRouter(prefix="/admin/resumes", tags=["admin-resumes"])


class ResumeEditRequest(BaseModel):
    version: int = Field(..., ge=1)
    fields: dict[str, Any] = Field(default_factory=dict)


class DelistRequest(BaseModel):
    version: int = Field(..., ge=1)
    reason: str = Field(default="manual_delist")


class ExtendRequest(BaseModel):
    version: int = Field(..., ge=1)
    days: int


def _collect_filters(
    gender: str | None, age_min: int | None, age_max: int | None,
    expected_cities: str | None, expected_job_categories: str | None,
    audit_status: str | None, owner_userid: str | None,
    created_from: datetime | None, created_to: datetime | None,
) -> dict:
    return {
        "gender": gender, "age_min": age_min, "age_max": age_max,
        "expected_cities": expected_cities,
        "expected_job_categories": expected_job_categories,
        "audit_status": audit_status, "owner_userid": owner_userid,
        "created_from": created_from, "created_to": created_to,
    }


@router.get("", summary="简历列表（admin）")
def list_resumes(
    gender: str | None = None,
    age_min: int | None = None,
    age_max: int | None = None,
    expected_cities: str | None = None,
    expected_job_categories: str | None = None,
    audit_status: str | None = None,
    owner_userid: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    sort: str = "created_at:desc",
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    filters = _collect_filters(
        gender, age_min, age_max, expected_cities, expected_job_categories,
        audit_status, owner_userid, created_from, created_to,
    )
    rows, total = resume_admin_service.list_resumes(db, filters, page, size, sort)
    return paged([ResumeRead.model_validate(r).model_dump(mode="json") for r in rows], total, page, size)


@router.get("/export", summary="简历导出 CSV")
def export_resumes(
    gender: str | None = None,
    age_min: int | None = None,
    age_max: int | None = None,
    expected_cities: str | None = None,
    expected_job_categories: str | None = None,
    audit_status: str | None = None,
    owner_userid: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    sort: str = "created_at:desc",
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    filters = _collect_filters(
        gender, age_min, age_max, expected_cities, expected_job_categories,
        audit_status, owner_userid, created_from, created_to,
    )
    rows = resume_admin_service.export_rows(db, filters, sort)
    headers = [
        "id", "owner_userid", "gender", "age",
        "expected_cities", "expected_job_categories",
        "salary_expect_floor_monthly",
        "accept_long_term", "accept_short_term",
        "audit_status", "audit_reason",
        "created_at", "expires_at", "version",
    ]
    body = []
    for r in rows:
        body.append([
            r.id, r.owner_userid, r.gender, r.age,
            r.expected_cities, r.expected_job_categories,
            r.salary_expect_floor_monthly,
            r.accept_long_term, r.accept_short_term,
            r.audit_status, r.audit_reason,
            r.created_at, r.expires_at, r.version,
        ])
    data = rows_to_csv_bytes(headers, body)
    filename = f"resumes_{datetime.now().strftime('%Y%m%d%H%M')}.csv"
    return Response(content=data, media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
    })


@router.get("/{resume_id}", summary="简历详情（admin）")
def get_resume(
    resume_id: int,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    r = resume_admin_service.get_resume(db, resume_id)
    return ok(ResumeRead.model_validate(r).model_dump(mode="json"))


@router.put("/{resume_id}", summary="简历编辑（带 version 乐观锁）")
def update_resume(
    resume_id: int,
    req: ResumeEditRequest,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    r = resume_admin_service.update_resume(db, resume_id, req.version, req.fields, current.username)
    return ok(ResumeRead.model_validate(r).model_dump(mode="json"))


@router.post("/{resume_id}/delist", summary="简历下架")
def delist_resume(
    resume_id: int,
    req: DelistRequest,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    resume_admin_service.delist(db, resume_id, req.version, req.reason, current.username)
    return ok()


@router.post("/{resume_id}/extend", summary="简历延期")
def extend_resume(
    resume_id: int,
    req: ExtendRequest,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    r = resume_admin_service.extend(db, resume_id, req.version, req.days, current.username)
    return ok({"expires_at": r.expires_at.isoformat() if r.expires_at else None})
