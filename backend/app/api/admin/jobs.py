"""岗位管理路由（Phase 5 模块 E）。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from sqlalchemy.orm import Session as _Session

from app.api.deps import get_db, require_admin_password_changed as require_admin
from app.core.csv_export import rows_to_csv_bytes
from app.core.responses import ok, paged
from app.models import AdminUser, User
from app.schemas.job import JobRead
from app.services import job_admin_service
from app.services.job_replace_service import REPLACEMENT_CANCEL_REASON_MAX_LENGTH
from app.services.storage_reference_service import storage_urls_for_response

router = APIRouter(prefix="/admin/jobs", tags=["admin-jobs"])


def _enrich_with_owner(db: _Session, jobs: list) -> dict[str, dict]:
    """根据 jobs 的 owner_userid 集合一次性查 user 表，返回 {userid: {phone, ...}}。

    用于在 admin 接口里给岗位详情/列表附带发布者信息（电话、公司、联系人、公司地址等）。
    分配给运营/管理员，三层数据隔离逻辑由专用的对外接口（wecom/miniprogram）负责，
    本接口默认全字段返回。
    """
    owner_ids = list({j.owner_userid for j in jobs if j.owner_userid})
    if not owner_ids:
        return {}
    rows = (
        db.query(
            User.external_userid, User.phone, User.company,
            User.contact_person, User.address, User.role, User.display_name,
        )
        .filter(User.external_userid.in_(owner_ids))
        .all()
    )
    return {
        r[0]: {
            "owner_phone": r[1],
            "owner_company": r[2],
            "owner_contact_person": r[3],
            "owner_address": r[4],
            "owner_role": r[5],
            "owner_display_name": r[6],
        }
        for r in rows
    }


def _job_to_dict(job, owner_map: dict[str, dict], projection: dict | None = None) -> dict:
    item = JobRead.model_validate(job).model_dump(mode="json")
    item["images"] = storage_urls_for_response(item.get("images"))
    # Optional S4 columns are exposed when the additive migration/model is
    # present, while mixed-version admin fleets continue to serialize safely.
    item.setdefault("aggregate_version", getattr(job, "aggregate_version", getattr(job, "version", None)))
    item.setdefault("event_status", getattr(job, "event_status", None))
    item.setdefault("trace_id", getattr(job, "trace_id", None))
    item.update(owner_map.get(job.owner_userid, {}))
    item.update(projection or {})
    return item


class JobEditRequest(BaseModel):
    version: int = Field(..., ge=1)
    fields: dict[str, Any] = Field(default_factory=dict)


class DelistRequest(BaseModel):
    version: int = Field(..., ge=1)
    reason: str = Field(..., description="manual_delist | filled")


class ExtendRequest(BaseModel):
    version: int = Field(..., ge=1)
    days: int = Field(..., description="15 或 30")


class RestoreRequest(BaseModel):
    version: int = Field(..., ge=1)


class ReplacementRetryRequest(BaseModel):
    old_job_version: int = Field(..., ge=1)
    reason: str = Field(..., min_length=1, max_length=200)


class ReplacementCancelRequest(BaseModel):
    reason: str = Field(
        default="operator_cancelled",
        min_length=1,
        max_length=REPLACEMENT_CANCEL_REASON_MAX_LENGTH,
    )


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
    lifecycle_scope: str | None = Query(None, description="active / candidate / history / all"),
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
    rows, total = job_admin_service.list_jobs(
        db, filters, page, size, sort, lifecycle_scope=lifecycle_scope,
    )
    owner_map = _enrich_with_owner(db, rows)
    projections = job_admin_service.replacement_projections(db, rows)
    return paged([
        _job_to_dict(r, owner_map, projections.get(r.id)) for r in rows
    ], total, page, size)


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
    lifecycle_scope: str | None = Query(None, description="active / candidate / history / all"),
    sort: str = "created_at:desc",
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    filters = _collect_filters(
        city, district, job_category, pay_type, audit_status, delist_reason,
        owner_userid, created_from, created_to, expires_from, expires_to,
        salary_min, salary_max,
    )
    rows = job_admin_service.export_rows(
        db, filters, sort, lifecycle_scope=lifecycle_scope,
    )
    owner_map = _enrich_with_owner(db, rows)
    projections = job_admin_service.replacement_projections(db, rows)
    headers = [
        "id", "owner_userid", "owner_company", "owner_contact_person", "owner_phone",
        "hiring_company", "city", "district", "address", "contact_person", "phone", "job_category",
        "salary_floor_monthly", "salary_ceiling_monthly", "pay_type",
        "headcount", "gender_required", "age_min", "age_max", "is_long_term",
        "audit_status", "audit_reason", "delist_reason", "activated_at",
        "candidate_expires_at", "replacement_id", "replacement_review_outcome",
        "replacement_lifecycle_status", "replacement_closed_reason",
        "replaces_job_id", "replaced_by_job_id", "created_at", "expires_at", "version",
        "aggregate_version", "event_status", "trace_id",
    ]
    body = []
    for r in rows:
        ow = owner_map.get(r.owner_userid, {})
        rp = projections.get(r.id, {})
        body.append([
            r.id, r.owner_userid,
            ow.get("owner_company"), ow.get("owner_contact_person"), ow.get("owner_phone"),
            r.hiring_company, r.city, r.district, r.address, r.contact_person, r.phone, r.job_category,
            r.salary_floor_monthly, r.salary_ceiling_monthly, r.pay_type,
            r.headcount, r.gender_required, r.age_min, r.age_max, r.is_long_term,
            r.audit_status, r.audit_reason, r.delist_reason, r.activated_at,
            r.candidate_expires_at, rp.get("replacement_id"),
            rp.get("replacement_review_outcome"), rp.get("replacement_lifecycle_status"),
            rp.get("replacement_closed_reason"), rp.get("replaces_job_id"),
            rp.get("replaced_by_job_id"),
            r.created_at, r.expires_at.isoformat() if r.expires_at else None, r.version,
            getattr(r, "aggregate_version", r.version), getattr(r, "event_status", None), getattr(r, "trace_id", None),
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
    owner_map = _enrich_with_owner(db, [job])
    projection = job_admin_service.replacement_projections(db, [job]).get(job.id)
    return ok(_job_to_dict(job, owner_map, projection))


@router.put("/{job_id}", summary="岗位编辑（带 version 乐观锁）")
def update_job(
    job_id: int,
    req: JobEditRequest,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    job = job_admin_service.update_job(db, job_id, req.version, req.fields, current.username)
    owner_map = _enrich_with_owner(db, [job])
    projection = job_admin_service.replacement_projections(db, [job]).get(job.id)
    return ok(_job_to_dict(job, owner_map, projection))


@router.post("/{job_id}/delist", summary="岗位下架")
def delist_job(
    job_id: int,
    req: DelistRequest,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    job_admin_service.delist(db, job_id, req.version, req.reason, current.username)
    return ok()


@router.post("/{job_id}/extend", summary="岗位延期")
def extend_job(
    job_id: int,
    req: ExtendRequest,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    job = job_admin_service.extend(db, job_id, req.version, req.days, current.username)
    return ok({"expires_at": job.expires_at.isoformat() if job.expires_at else None})


@router.post("/{job_id}/restore", summary="岗位取消下架")
def restore_job(
    job_id: int,
    req: RestoreRequest,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    job_admin_service.restore(db, job_id, req.version, current.username)
    return ok()


@router.post("/replacements/{replacement_id}/retry", summary="重试岗位替换激活")
def retry_replacement(
    replacement_id: int,
    req: ReplacementRetryRequest,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    from app.services.job_replace_service import retry_activation

    activated = retry_activation(
        db,
        replacement_id,
        req.old_job_version,
        operator=current.username,
        reason=req.reason,
    )
    db.commit()
    return ok({"activated": activated})


@router.post("/replacements/{replacement_id}/cancel", summary="取消岗位替换候选")
def cancel_replacement(
    replacement_id: int,
    req: ReplacementCancelRequest,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    from app.services.job_replace_service import cancel_candidate

    cancel_candidate(
        db,
        replacement_id,
        operator=current.username,
        reason=req.reason,
    )
    db.commit()
    return ok()
