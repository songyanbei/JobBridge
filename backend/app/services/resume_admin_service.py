"""简历管理 service（Phase 5 模块 E）。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import json

from sqlalchemy import asc, desc, func
from sqlalchemy.orm import Query, Session

from app.core.exceptions import BusinessException
from app.models import Resume, SystemConfig
from app.services.admin_log_service import _json_safe, write_admin_log


_FILTER_WHITELIST = {
    "gender", "audit_status", "owner_userid",
}

_SORT_WHITELIST = {
    "created_at", "updated_at", "expires_at", "age",
    "salary_expect_floor_monthly", "id",
}

_EDIT_WHITELIST = {
    "expected_cities", "expected_job_categories",
    "salary_expect_floor_monthly", "gender", "age",
    "accept_long_term", "accept_short_term",
    "expected_districts", "height", "weight", "education",
    "work_experience", "accept_night_shift", "accept_standing_work",
    "accept_overtime", "accept_outside_province",
    "couple_seeking_together", "has_health_certificate",
    "ethnicity", "available_from", "has_tattoo", "taboo",
    "description",
}


def _load_config_int(db: Session, key: str, default: int) -> int:
    cfg = db.query(SystemConfig).filter(SystemConfig.config_key == key).first()
    try:
        return int(cfg.config_value) if cfg else default
    except (TypeError, ValueError):
        return default


def _apply_filters(query: Query, filters: dict[str, Any]) -> Query:
    for key in _FILTER_WHITELIST:
        v = filters.get(key)
        if v is None or v == "":
            continue
        query = query.filter(getattr(Resume, key) == v)

    if filters.get("age_min") is not None:
        query = query.filter(Resume.age >= int(filters["age_min"]))
    if filters.get("age_max") is not None:
        query = query.filter(Resume.age <= int(filters["age_max"]))
    if filters.get("created_from"):
        query = query.filter(Resume.created_at >= filters["created_from"])
    if filters.get("created_to"):
        query = query.filter(Resume.created_at <= filters["created_to"])

    # JSON 列筛选（期望城市 / 工种）：用 json.dumps 做严格序列化 + 参数化绑定，
    # 杜绝把原始字符串拼进 SQL 字面量造成的 JSON/注入风险。
    exp_cities = filters.get("expected_cities")
    if exp_cities:
        query = query.filter(
            func.json_contains(Resume.expected_cities, json.dumps(str(exp_cities), ensure_ascii=False))
        )
    exp_cats = filters.get("expected_job_categories")
    if exp_cats:
        query = query.filter(
            func.json_contains(Resume.expected_job_categories, json.dumps(str(exp_cats), ensure_ascii=False))
        )
    return query


def _apply_sort(query: Query, sort: str | None) -> Query:
    if not sort:
        return query.order_by(Resume.created_at.desc())
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
        col = getattr(Resume, field)
        clauses.append(desc(col) if order == "desc" else asc(col))
    if clauses:
        return query.order_by(*clauses)
    return query.order_by(Resume.created_at.desc())


def list_resumes(
    db: Session,
    filters: dict[str, Any],
    page: int = 1,
    size: int = 20,
    sort: str | None = None,
    include_deleted: bool = False,
) -> tuple[list[Resume], int]:
    page = max(1, page)
    size = max(1, min(size, 100))
    query = db.query(Resume)
    if not include_deleted:
        query = query.filter(Resume.deleted_at.is_(None))
    query = _apply_filters(query, filters)
    total = query.count()
    query = _apply_sort(query, sort)
    rows = query.offset((page - 1) * size).limit(size).all()
    return rows, total


def get_resume(db: Session, resume_id: int) -> Resume:
    resume = db.query(Resume).filter(Resume.id == resume_id).first()
    if not resume:
        raise BusinessException(40401, "简历不存在")
    return resume


def _snapshot(resume: Resume) -> dict:
    keys = _EDIT_WHITELIST | {"audit_status", "expires_at", "version"}
    return {k: _json_safe(getattr(resume, k, None)) for k in keys}


def update_resume(db: Session, resume_id: int, version: int, payload: dict, operator: str) -> Resume:
    resume = get_resume(db, resume_id)
    if int(resume.version or 0) != int(version):
        raise BusinessException(40902, "此条目已被修改，请刷新",
                                {"current_version": int(resume.version or 0)})

    unknown = [k for k in payload.keys() if k not in _EDIT_WHITELIST]
    if unknown:
        raise BusinessException(40101, f"不允许编辑的字段: {','.join(unknown)}")

    before = _snapshot(resume)

    # 原子 UPDATE + version 递增
    new_version = int(version) + 1
    patch = {**payload, "version": new_version}
    rowcount = (
        db.query(Resume)
        .filter(Resume.id == resume_id, Resume.version == version)
        .update(patch, synchronize_session=False)
    )
    if rowcount == 0:
        db.rollback()
        current = db.query(Resume).filter(Resume.id == resume_id).first()
        raise BusinessException(
            40902, "此条目已被修改，请刷新",
            {"current_version": int(current.version) if current else 0},
        )
    resume = db.query(Resume).filter(Resume.id == resume_id).first()
    after = _snapshot(resume)

    write_admin_log(
        db,
        target_type="resume", target_id=resume.id,
        action="manual_edit", operator=operator,
        before=before, after=after,
    )
    db.commit()
    db.refresh(resume)
    return resume


def delist(db: Session, resume_id: int, reason: str, operator: str) -> None:
    """简历软下架：置 deleted_at（简历没有 delist_reason 字段）。"""
    resume = get_resume(db, resume_id)
    if resume.deleted_at is not None:
        raise BusinessException(40904, "简历已下架")
    before = _snapshot(resume)
    resume.deleted_at = datetime.now()
    write_admin_log(
        db,
        target_type="resume", target_id=resume.id,
        action="manual_edit", operator=operator,
        before=before, after=_snapshot(resume) | {"deleted_at": resume.deleted_at.isoformat()},
        reason=f"delist:{reason}",
    )
    db.commit()


def extend(db: Session, resume_id: int, days: int, operator: str) -> Resume:
    if days not in (15, 30):
        raise BusinessException(40101, "延期天数仅支持 15 或 30")
    resume = get_resume(db, resume_id)
    before = _snapshot(resume)
    now = datetime.now()
    max_days = _load_config_int(db, "ttl.resume.days", 30) * 2
    base = resume.expires_at if resume.expires_at and resume.expires_at > now else now
    new_expires = base + timedelta(days=days)
    ceiling = (resume.created_at or now) + timedelta(days=max_days)
    if new_expires > ceiling:
        new_expires = ceiling
    resume.expires_at = new_expires

    write_admin_log(
        db,
        target_type="resume", target_id=resume.id,
        action="manual_edit", operator=operator,
        before=before, after=_snapshot(resume), reason=f"extend:{days}d",
    )
    db.commit()
    db.refresh(resume)
    return resume


def export_rows(db: Session, filters: dict[str, Any], sort: str | None = None, limit: int = 10000) -> list[Resume]:
    query = db.query(Resume).filter(Resume.deleted_at.is_(None))
    query = _apply_filters(query, filters)
    query = _apply_sort(query, sort)
    count = query.count()
    if count > limit:
        raise BusinessException(40101, f"导出条数超过上限 {limit}，请分批导出")
    return query.all()
