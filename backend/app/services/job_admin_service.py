"""岗位管理 service（Phase 5 模块 E）。

仅做 admin 视角的列表 / 详情 / 编辑 / 下架 / 延期 / 取消下架 / 导出。
审核动作走 audit_workbench_service；本模块涉及的审核状态变化不算入。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import asc, desc
from sqlalchemy.orm import Query, Session

from app.core.exceptions import BusinessException
from app.models import Job, SystemConfig
from app.services.admin_log_service import _json_safe, write_admin_log


# ---------------------------------------------------------------------------
# 白名单
# ---------------------------------------------------------------------------

_FILTER_WHITELIST = {
    "city", "district", "job_category", "pay_type",
    "audit_status", "delist_reason", "owner_userid",
}

_SORT_WHITELIST = {
    "created_at", "updated_at", "expires_at",
    "salary_floor_monthly", "id",
}

_EDIT_WHITELIST = {
    "city", "district", "address", "job_category", "job_sub_category",
    "salary_floor_monthly", "salary_ceiling_monthly",
    "pay_type", "headcount", "gender_required",
    "age_min", "age_max", "is_long_term",
    "provide_meal", "provide_housing", "dorm_condition",
    "shift_pattern", "work_hours",
    "accept_couple", "accept_student", "accept_minority",
    "height_required", "experience_required", "education_required",
    "rebate", "employment_type", "contract_type",
    "min_duration", "description",
}


def _load_config_int(db: Session, key: str, default: int) -> int:
    cfg = db.query(SystemConfig).filter(SystemConfig.config_key == key).first()
    try:
        return int(cfg.config_value) if cfg else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 列表 / 详情
# ---------------------------------------------------------------------------

def _apply_filters(query: Query, filters: dict[str, Any]) -> Query:
    for key in _FILTER_WHITELIST:
        v = filters.get(key)
        if v is None or v == "":
            continue
        query = query.filter(getattr(Job, key) == v)

    # 范围查询
    if filters.get("salary_min") is not None:
        query = query.filter(Job.salary_floor_monthly >= int(filters["salary_min"]))
    if filters.get("salary_max") is not None:
        query = query.filter(Job.salary_floor_monthly <= int(filters["salary_max"]))
    if filters.get("created_from"):
        query = query.filter(Job.created_at >= filters["created_from"])
    if filters.get("created_to"):
        query = query.filter(Job.created_at <= filters["created_to"])
    if filters.get("expires_from"):
        query = query.filter(Job.expires_at >= filters["expires_from"])
    if filters.get("expires_to"):
        query = query.filter(Job.expires_at <= filters["expires_to"])
    return query


def _apply_sort(query: Query, sort: str | None) -> Query:
    if not sort:
        return query.order_by(Job.created_at.desc())
    clauses = []
    for frag in sort.split(","):
        frag = frag.strip()
        if not frag:
            continue
        field, _, order = frag.partition(":")
        field = field.strip()
        order = order.strip().lower() or "asc"
        if field not in _SORT_WHITELIST:
            raise BusinessException(40101, f"不允许排序字段: {field}")
        col = getattr(Job, field)
        clauses.append(desc(col) if order == "desc" else asc(col))
    if clauses:
        return query.order_by(*clauses)
    return query.order_by(Job.created_at.desc())


def list_jobs(
    db: Session,
    filters: dict[str, Any],
    page: int = 1,
    size: int = 20,
    sort: str | None = None,
    include_deleted: bool = False,
) -> tuple[list[Job], int]:
    page = max(1, page)
    size = max(1, min(size, 100))
    query = db.query(Job)
    if not include_deleted:
        query = query.filter(Job.deleted_at.is_(None))
    query = _apply_filters(query, filters)
    total = query.count()
    query = _apply_sort(query, sort)
    rows = query.offset((page - 1) * size).limit(size).all()
    return rows, total


def get_job(db: Session, job_id: int) -> Job:
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise BusinessException(40401, "岗位不存在")
    return job


# ---------------------------------------------------------------------------
# 编辑
# ---------------------------------------------------------------------------

def _snapshot(job: Job) -> dict:
    keys = _EDIT_WHITELIST | {"audit_status", "delist_reason", "expires_at", "version"}
    return {k: _json_safe(getattr(job, k, None)) for k in keys}


def update_job(db: Session, job_id: int, version: int, payload: dict, operator: str) -> Job:
    job = get_job(db, job_id)
    if int(job.version or 0) != int(version):
        raise BusinessException(40902, "此条目已被修改，请刷新",
                                {"current_version": int(job.version or 0)})

    unknown = [k for k in payload.keys() if k not in _EDIT_WHITELIST]
    if unknown:
        raise BusinessException(40101, f"不允许编辑的字段: {','.join(unknown)}")

    before = _snapshot(job)

    # 原子 UPDATE + version 递增（WHERE id=? AND version=?，rowcount=0 则乐观锁失败）
    new_version = int(version) + 1
    patch = {**payload, "version": new_version}
    rowcount = (
        db.query(Job)
        .filter(Job.id == job_id, Job.version == version)
        .update(patch, synchronize_session=False)
    )
    if rowcount == 0:
        db.rollback()
        current = db.query(Job).populate_existing().filter(Job.id == job_id).first()
        raise BusinessException(
            40902, "此条目已被修改，请刷新",
            {"current_version": int(current.version) if current else 0},
        )
    # populate_existing 避免 synchronize_session=False 与 identity map 组合下返回旧值
    job = db.query(Job).populate_existing().filter(Job.id == job_id).first()
    after = _snapshot(job)

    write_admin_log(
        db,
        target_type="job", target_id=job.id,
        action="manual_edit", operator=operator,
        before=before, after=after,
    )
    db.commit()
    return job


# ---------------------------------------------------------------------------
# 下架 / 延期 / 取消下架
# ---------------------------------------------------------------------------

def _atomic_job_update(db: Session, job_id: int, expected_version: int, patch: dict) -> Job:
    """共用的原子 UPDATE + version 递增 + populate_existing 刷新。"""
    new_version = int(expected_version) + 1
    body = {**patch, "version": new_version}
    rowcount = (
        db.query(Job)
        .filter(Job.id == job_id, Job.version == expected_version)
        .update(body, synchronize_session=False)
    )
    if rowcount == 0:
        db.rollback()
        current = db.query(Job).filter(Job.id == job_id).first()
        raise BusinessException(
            40902, "此条目已被修改，请刷新",
            {"current_version": int(current.version) if current else 0},
        )
    # populate_existing 让 identity-mapped 的 Job 实例从 DB 重新加载属性，
    # 避免 after 快照拿到 synchronize_session=False 后的旧值。
    job = db.query(Job).populate_existing().filter(Job.id == job_id).first()
    return job


def delist(db: Session, job_id: int, version: int, reason: str, operator: str) -> None:
    if reason not in ("manual_delist", "filled"):
        raise BusinessException(40101, "无效的下架原因")
    job = get_job(db, job_id)
    if int(job.version or 0) != int(version):
        raise BusinessException(40902, "此条目已被修改，请刷新",
                                {"current_version": int(job.version or 0)})
    before = _snapshot(job)
    job = _atomic_job_update(db, job_id, version, {"delist_reason": reason})
    write_admin_log(
        db,
        target_type="job", target_id=job.id,
        action="manual_edit", operator=operator,
        before=before, after=_snapshot(job), reason=f"delist:{reason}",
    )
    db.commit()


def extend(db: Session, job_id: int, version: int, days: int, operator: str) -> Job:
    if days not in (15, 30):
        raise BusinessException(40101, "延期天数仅支持 15 或 30")
    job = get_job(db, job_id)
    if int(job.version or 0) != int(version):
        raise BusinessException(40902, "此条目已被修改，请刷新",
                                {"current_version": int(job.version or 0)})

    before = _snapshot(job)
    now = datetime.now()
    max_days = _load_config_int(db, "ttl.job.days", 30) * 2
    base = job.expires_at if job.expires_at and job.expires_at > now else now
    new_expires = base + timedelta(days=days)
    ceiling = (job.created_at or now) + timedelta(days=max_days)
    if new_expires > ceiling:
        new_expires = ceiling

    job = _atomic_job_update(db, job_id, version, {"expires_at": new_expires})
    write_admin_log(
        db,
        target_type="job", target_id=job.id,
        action="manual_edit", operator=operator,
        before=before, after=_snapshot(job), reason=f"extend:{days}d",
    )
    db.commit()
    return job


def restore(db: Session, job_id: int, version: int, operator: str) -> None:
    job = get_job(db, job_id)
    if int(job.version or 0) != int(version):
        raise BusinessException(40902, "此条目已被修改，请刷新",
                                {"current_version": int(job.version or 0)})
    if not job.delist_reason:
        raise BusinessException(40904, "岗位未下架")
    if job.expires_at and job.expires_at <= datetime.now():
        raise BusinessException(40904, "岗位已过期，无法取消下架")

    before = _snapshot(job)
    job = _atomic_job_update(db, job_id, version, {"delist_reason": None})
    write_admin_log(
        db,
        target_type="job", target_id=job.id,
        action="reinstate", operator=operator,
        before=before, after=_snapshot(job), reason="restore",
    )
    db.commit()


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------

def export_rows(db: Session, filters: dict[str, Any], sort: str | None = None, limit: int = 10000) -> list[Job]:
    query = db.query(Job).filter(Job.deleted_at.is_(None))
    query = _apply_filters(query, filters)
    query = _apply_sort(query, sort)
    count = query.count()
    if count > limit:
        raise BusinessException(40101, f"导出条数超过上限 {limit}，请分批导出")
    return query.all()
