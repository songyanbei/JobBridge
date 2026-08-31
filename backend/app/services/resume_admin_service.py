"""简历管理 service（Phase 5 模块 E）。"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

import json

from sqlalchemy import and_, asc, desc, func, or_
from sqlalchemy.orm import Query, Session

from app.core.exceptions import BusinessException
from app.models import Resume, ResumeReplacement, SystemConfig
from app.services.admin_log_service import _json_safe, write_admin_log
from app.services.resume_mutation_service import (
    append_resume_domain_event, close_active_replacement, increment_resume_version, lock_resume,
    reject_if_replacement_in_progress, resume_is_online, utc_now_naive,
)


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

_LIFECYCLE_SCOPES = {"active", "candidate", "history", "all"}


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


def _apply_lifecycle_scope(query: Query, lifecycle_scope: str | None) -> Query:
    """Apply mutually-exclusive lifecycle views.

    ``None`` intentionally preserves the pre-stage-6 admin API behaviour: all
    non-deleted rows.  Operators opt into the stricter lifecycle views.
    """
    if lifecycle_scope is not None and lifecycle_scope not in _LIFECYCLE_SCOPES:
        raise BusinessException(40101, "无效的 lifecycle_scope")
    if lifecycle_scope in (None, "all"):
        return query
    now = utc_now_naive()
    active = and_(
        Resume.audit_status == "passed",
        Resume.activated_at.isnot(None),
        Resume.expires_at.isnot(None),
        Resume.expires_at > now,
        Resume.delist_reason.is_(None),
        Resume.deleted_at.is_(None),
    )
    candidate = and_(
        Resume.audit_status.in_(("pending", "rejected")),
        Resume.activated_at.is_(None),
        Resume.expires_at.is_(None),
        Resume.candidate_expires_at.isnot(None),
        Resume.candidate_expires_at > now,
        Resume.deleted_at.is_(None),
    )
    if lifecycle_scope == "active":
        return query.filter(active)
    if lifecycle_scope == "candidate":
        return query.filter(candidate)
    return query.filter(or_(
        Resume.deleted_at.isnot(None),
        Resume.delist_reason.isnot(None),
        and_(Resume.expires_at.isnot(None), Resume.expires_at <= now),
        and_(
            Resume.activated_at.is_(None),
            Resume.candidate_expires_at.isnot(None),
            Resume.candidate_expires_at <= now,
        ),
    ))


def list_resumes(
    db: Session,
    filters: dict[str, Any],
    page: int = 1,
    size: int = 20,
    sort: str | None = None,
    include_deleted: bool = False,
    lifecycle_scope: str | None = None,
) -> tuple[list[Resume], int]:
    page = max(1, page)
    size = max(1, min(size, 100))
    query = db.query(Resume)
    if not include_deleted and lifecycle_scope != "history":
        query = query.filter(Resume.deleted_at.is_(None))
    query = _apply_lifecycle_scope(query, lifecycle_scope)
    query = _apply_filters(query, filters)
    total = query.count()
    query = _apply_sort(query, sort)
    rows = query.offset((page - 1) * size).limit(size).all()
    return rows, total


def replacement_projections(db: Session, resumes: list[Resume]) -> dict[int, dict]:
    resume_ids = {int(row.id) for row in resumes}
    if not resume_ids:
        return {}
    relations = db.query(ResumeReplacement).filter(
        (ResumeReplacement.old_resume_id.in_(resume_ids))
        | (ResumeReplacement.new_resume_id.in_(resume_ids))
    ).order_by(ResumeReplacement.created_at.desc(), ResumeReplacement.id.desc()).all()
    incoming: dict[int, ResumeReplacement] = {}
    outgoing: dict[int, ResumeReplacement] = {}
    for relation in relations:
        if int(relation.new_resume_id) in resume_ids:
            incoming.setdefault(int(relation.new_resume_id), relation)
        if int(relation.old_resume_id) in resume_ids:
            outgoing.setdefault(int(relation.old_resume_id), relation)
    result: dict[int, dict] = {}
    for resume_id in resume_ids:
        before = incoming.get(resume_id)
        after = outgoing.get(resume_id)
        relation = before or after
        if relation is None:
            continue
        result[resume_id] = {
            "replacement_id": int(relation.id),
            "replacement_review_outcome": relation.review_outcome,
            "replacement_lifecycle_status": relation.lifecycle_status,
            "replacement_closed_reason": relation.closed_reason,
            "replacement_conflict_reason": relation.conflict_reason,
            "replaces_resume_id": int(before.old_resume_id) if before else None,
            "replaced_by_resume_id": int(after.new_resume_id) if after else None,
        }
    return result


def get_resume(db: Session, resume_id: int) -> Resume:
    resume = db.query(Resume).filter(Resume.id == resume_id).first()
    if not resume:
        raise BusinessException(40401, "简历不存在")
    return resume


def _snapshot(resume: Resume) -> dict:
    keys = _EDIT_WHITELIST | {"audit_status", "expires_at", "version"}
    return {k: _json_safe(getattr(resume, k, None)) for k in keys}


def update_resume(db: Session, resume_id: int, version: int, payload: dict, operator: str) -> Resume:
    from app.services.resume_cutover_service import assert_resume_writes_allowed

    assert_resume_writes_allowed()
    resume = lock_resume(db, resume_id)
    if int(resume.version or 0) != int(version):
        raise BusinessException(40902, "此条目已被修改，请刷新",
                                {"current_version": int(resume.version or 0)})

    unknown = [k for k in payload.keys() if k not in _EDIT_WHITELIST]
    if unknown:
        raise BusinessException(40101, f"不允许编辑的字段: {','.join(unknown)}")
    if not resume_is_online(resume, now=utc_now_naive(), strict=True):
        raise BusinessException(40904, "resume_not_active")
    reject_if_replacement_in_progress(db, resume_id)

    before = _snapshot(resume)

    # 原子 UPDATE + version 递增
    new_version = int(version) + 1
    current_aggregate = int(getattr(resume, "aggregate_version", None) or 0)
    patch = {
        **payload,
        "version": new_version,
        "aggregate_version": max(current_aggregate, int(version)) + 1,
    }
    rowcount = (
        db.query(Resume)
        .filter(
            Resume.id == resume_id,
            Resume.version == version,
            Resume.aggregate_version == current_aggregate,
        )
        .update(patch, synchronize_session=False)
    )
    if rowcount == 0:
        db.rollback()
        current = db.query(Resume).populate_existing().filter(Resume.id == resume_id).first()
        raise BusinessException(
            40902, "此条目已被修改，请刷新",
            {"current_version": int(current.version) if current else 0},
        )
    # populate_existing 避免 synchronize_session=False 与 identity map 组合下返回旧值
    resume = db.query(Resume).populate_existing().filter(Resume.id == resume_id).first()
    after = _snapshot(resume)

    write_admin_log(
        db,
        target_type="resume", target_id=resume.id,
        action="manual_edit", operator=operator,
        before=before, after=after,
    )
    append_resume_domain_event(
        db, resume, "resume.updated",
        payload={"status": "updated", "reason_code": "manual_edit"},
    )
    db.commit()
    return resume


def _atomic_resume_update(db: Session, resume_id: int, expected_version: int, patch: dict) -> Resume:
    new_version = int(expected_version) + 1
    body = {**patch, "version": new_version}
    rowcount = (
        db.query(Resume)
        .filter(Resume.id == resume_id, Resume.version == expected_version)
        .update(body, synchronize_session=False)
    )
    if rowcount == 0:
        db.rollback()
        current = db.query(Resume).filter(Resume.id == resume_id).first()
        raise BusinessException(
            40902, "此条目已被修改，请刷新",
            {"current_version": int(current.version) if current else 0},
        )
    resume = db.query(Resume).populate_existing().filter(Resume.id == resume_id).first()
    return resume


def delist(db: Session, resume_id: int, version: int, reason: str, operator: str) -> None:
    """Close any candidate and delist the active resume in one transaction."""
    from app.services.resume_cutover_service import assert_resume_writes_allowed

    assert_resume_writes_allowed()
    resume = close_active_replacement(db, resume_id, reason="old_resume_delisted")
    if int(resume.version or 0) != int(version):
        raise BusinessException(40902, "此条目已被修改，请刷新",
                                {"current_version": int(resume.version or 0)})
    if not resume_is_online(resume, now=utc_now_naive(), strict=True):
        raise BusinessException(40904, "简历已下架")
    before = _snapshot(resume)
    now = utc_now_naive()
    resume.deleted_at = now
    resume.delist_reason = "manual_delist"
    increment_resume_version(resume)
    from app.services.target_cleanup_service import ensure_target_cleanup_task
    ensure_target_cleanup_task(db, "resume", resume_id, reason="manual_delist")
    write_admin_log(
        db,
        target_type="resume", target_id=resume.id,
        action="manual_edit", operator=operator,
        before=before,
        after=_snapshot(resume) | {"deleted_at": now.isoformat()},
        reason=f"delist:{reason}",
    )
    append_resume_domain_event(
        db, resume, "resume.delisted",
        payload={"status": "delisted", "reason_code": "manual_delist"}, tombstone=True,
    )
    db.commit()


def extend(db: Session, resume_id: int, version: int, days: int, operator: str) -> Resume:
    from app.services.resume_cutover_service import assert_resume_writes_allowed

    assert_resume_writes_allowed()
    if days not in (15, 30):
        raise BusinessException(40101, "延期天数仅支持 15 或 30")
    resume = lock_resume(db, resume_id)
    if int(resume.version or 0) != int(version):
        raise BusinessException(40902, "此条目已被修改，请刷新",
                                {"current_version": int(resume.version or 0)})

    now = utc_now_naive()
    if not resume_is_online(resume, now=now, strict=True):
        raise BusinessException(40904, "resume_not_active")
    reject_if_replacement_in_progress(db, resume_id)
    before = _snapshot(resume)
    ttl_days = _load_config_int(db, "ttl.resume.days", 30)
    base = max(resume.expires_at, now)
    ceiling = max(resume.expires_at, resume.activated_at + timedelta(days=2 * ttl_days))
    if base >= ceiling:
        raise BusinessException(40904, "extension_limit_reached")
    new_expires = min(base + timedelta(days=days), ceiling)
    resume.expires_at = new_expires
    increment_resume_version(resume)
    write_admin_log(
        db,
        target_type="resume", target_id=resume.id,
        action="manual_edit", operator=operator,
        before=before, after=_snapshot(resume), reason=f"extend:{days}d",
    )
    append_resume_domain_event(
        db, resume, "resume.updated",
        payload={"reason": "extend", "days": int(days)},
    )
    db.commit()
    return resume


def export_rows(db: Session, filters: dict[str, Any], sort: str | None = None, limit: int = 10000) -> list[Resume]:
    query = db.query(Resume).filter(Resume.deleted_at.is_(None))
    query = _apply_filters(query, filters)
    query = _apply_sort(query, sort)
    count = query.count()
    if count > limit:
        raise BusinessException(40101, f"导出条数超过上限 {limit}，请分批导出")
    return query.all()
