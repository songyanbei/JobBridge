"""审核工作台 service（Phase 5 模块 C）。

职责：
- 队列列表 / 待审数量 / 详情聚合（含 7 天提交历史、风险等级）
- 软锁（Redis）+ 乐观锁（DB version）
- 通过 / 驳回 / 编辑 / Undo 的业务编排 + audit_log 写入

依赖：复用 Phase 3 `audit_service._scan_sensitive_words` 做风险判定。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.exceptions import BusinessException
from app.core.redis_client import (
    acquire_audit_lock,
    get_audit_lock_holder,
    pop_undo,
    refresh_audit_lock,
    release_audit_lock,
    save_undo,
)
from app.models import AuditLog, Job, Resume, User
from app.services import audit_service
from app.services.admin_log_service import _json_safe, write_admin_log


# ---------------------------------------------------------------------------
# 字段白名单：决定编辑接口允许修改哪些字段
# ---------------------------------------------------------------------------

_JOB_EDIT_FIELDS = {
    "city", "district", "job_category", "job_sub_category",
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

_RESUME_EDIT_FIELDS = {
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

_JOB_SNAPSHOT_FIELDS = _JOB_EDIT_FIELDS | {"audit_status", "delist_reason", "expires_at", "version"}
_RESUME_SNAPSHOT_FIELDS = _RESUME_EDIT_FIELDS | {"audit_status", "expires_at", "version"}


def _model_for(target_type: str):
    if target_type == "job":
        return Job, _JOB_EDIT_FIELDS, _JOB_SNAPSHOT_FIELDS
    if target_type == "resume":
        return Resume, _RESUME_EDIT_FIELDS, _RESUME_SNAPSHOT_FIELDS
    raise BusinessException(40101, f"不支持的 target_type: {target_type}")


def _load(db: Session, target_type: str, target_id: int):
    model, _, _ = _model_for(target_type)
    obj = db.query(model).filter(model.id == target_id).first()
    if not obj:
        raise BusinessException(40401, "审核对象不存在")
    return obj


def _snapshot(obj, target_type: str) -> dict:
    _, _, snap_fields = _model_for(target_type)
    result: dict[str, Any] = {}
    for f in snap_fields:
        result[f] = _json_safe(getattr(obj, f, None))
    return result


def _check_version(obj, expected_version: int) -> None:
    if int(obj.version or 0) != int(expected_version):
        raise BusinessException(40902, "此条目已被修改，请刷新",
                                {"current_version": int(obj.version or 0)})


# ---------------------------------------------------------------------------
# 队列 / 待审数
# ---------------------------------------------------------------------------

@dataclass
class QueueItem:
    target_type: str
    obj: Any  # Job / Resume
    risk_level: str
    locked_by: str | None


def list_queue(
    db: Session,
    status: str = "pending",
    target_type: str | None = None,
    page: int = 1,
    size: int = 20,
) -> tuple[list[QueueItem], int]:
    if status not in ("pending", "passed", "rejected"):
        raise BusinessException(40101, "无效的 status")
    if target_type and target_type not in ("job", "resume"):
        raise BusinessException(40101, "无效的 target_type")
    if page < 1:
        page = 1
    size = max(1, min(size, 100))
    offset = (page - 1) * size

    targets = [target_type] if target_type else ["job", "resume"]

    # 分两阶段：先各自取到 offset+size 内按时间倒序的候选 id+created_at；
    # 在 Python 层合并排序，再截取当前 page 窗口，最后按 id 回表取对象；
    # 保证跨类型分页时不丢数据、不做全表加载。
    per_type_candidates: list[tuple[str, int, datetime]] = []
    total = 0
    need = offset + size
    for tt in targets:
        model, _, _ = _model_for(tt)
        base = db.query(model).filter(model.audit_status == status)
        if hasattr(model, "deleted_at"):
            base = base.filter(model.deleted_at.is_(None))
        total += base.count()
        rows = (
            base.with_entities(model.id, model.created_at)
            .order_by(model.created_at.desc(), model.id.desc())
            .limit(need)
            .all()
        )
        for rid, rct in rows:
            per_type_candidates.append((tt, rid, rct))

    # 全局按 created_at desc + id desc 排序
    per_type_candidates.sort(key=lambda x: (x[2], x[1]), reverse=True)
    window = per_type_candidates[offset: offset + size]

    items: list[QueueItem] = []
    # 按类型聚合回表，减少往返
    by_type: dict[str, list[int]] = {"job": [], "resume": []}
    order_key: list[tuple[str, int]] = []
    for tt, rid, _ in window:
        by_type.setdefault(tt, []).append(rid)
        order_key.append((tt, rid))

    obj_map: dict[tuple[str, int], Any] = {}
    for tt, ids in by_type.items():
        if not ids:
            continue
        model, _, _ = _model_for(tt)
        rows = db.query(model).filter(model.id.in_(ids)).all()
        for row in rows:
            obj_map[(tt, row.id)] = row

    for tt, rid in order_key:
        obj = obj_map.get((tt, rid))
        if obj is None:
            continue
        risk, _ = _risk_level(obj, db)
        holder = None
        try:
            holder = get_audit_lock_holder(tt, obj.id)
        except Exception:
            pass
        items.append(QueueItem(target_type=tt, obj=obj, risk_level=risk, locked_by=holder))

    return items, total


def get_pending_count(db: Session) -> dict:
    job_count = db.query(Job).filter(Job.audit_status == "pending", Job.deleted_at.is_(None)).count()
    resume_count = db.query(Resume).filter(Resume.audit_status == "pending", Resume.deleted_at.is_(None)).count()
    return {"job": job_count, "resume": resume_count, "total": job_count + resume_count}


# ---------------------------------------------------------------------------
# 风险等级（复用 Phase 3 敏感词扫描）
# ---------------------------------------------------------------------------

def _risk_level(obj, db: Session) -> tuple[str, list[str]]:
    text = (obj.raw_text or "") + "\n" + (getattr(obj, "description", "") or "")
    hits = audit_service._scan_sensitive_words(text, db)
    if not hits:
        return "low", []
    levels = {h.get("level") for h in hits}
    triggers = [f"敏感词:{h.get('word')}" for h in hits]
    if "high" in levels:
        return "high", triggers
    if "mid" in levels:
        return "mid", triggers
    return "low", triggers


def _submitter_history(db: Session, owner_userid: str) -> dict:
    """聚合某提交者近 7 天的审核历史。

    策略：取该用户近 7 天内提交的 job / resume 的 id 集合，再按 id
    过滤 audit_log 的 manual_pass / manual_reject / auto_pass / auto_reject 动作。
    """
    seven_days = datetime.now() - timedelta(days=7)

    job_ids = [
        str(i) for (i,) in db.query(Job.id).filter(
            Job.owner_userid == owner_userid, Job.created_at >= seven_days,
        ).all()
    ]
    resume_ids = [
        str(i) for (i,) in db.query(Resume.id).filter(
            Resume.owner_userid == owner_userid, Resume.created_at >= seven_days,
        ).all()
    ]

    passed = 0
    rejected = 0
    if job_ids:
        passed += db.query(AuditLog).filter(
            AuditLog.target_type == "job",
            AuditLog.target_id.in_(job_ids),
            AuditLog.action.in_(["auto_pass", "manual_pass"]),
            AuditLog.created_at >= seven_days,
        ).count()
        rejected += db.query(AuditLog).filter(
            AuditLog.target_type == "job",
            AuditLog.target_id.in_(job_ids),
            AuditLog.action.in_(["auto_reject", "manual_reject"]),
            AuditLog.created_at >= seven_days,
        ).count()
    if resume_ids:
        passed += db.query(AuditLog).filter(
            AuditLog.target_type == "resume",
            AuditLog.target_id.in_(resume_ids),
            AuditLog.action.in_(["auto_pass", "manual_pass"]),
            AuditLog.created_at >= seven_days,
        ).count()
        rejected += db.query(AuditLog).filter(
            AuditLog.target_type == "resume",
            AuditLog.target_id.in_(resume_ids),
            AuditLog.action.in_(["auto_reject", "manual_reject"]),
            AuditLog.created_at >= seven_days,
        ).count()

    return {
        "passed": passed,
        "rejected": rejected,
        "last_7d": {"job": len(job_ids), "resume": len(resume_ids)},
    }


def get_detail(db: Session, target_type: str, target_id: int) -> dict:
    obj = _load(db, target_type, target_id)
    risk, triggers = _risk_level(obj, db)
    holder = None
    try:
        holder = get_audit_lock_holder(target_type, target_id)
    except Exception:
        pass

    extracted: dict[str, Any] = {}
    for f in (_JOB_SNAPSHOT_FIELDS if target_type == "job" else _RESUME_SNAPSHOT_FIELDS):
        extracted[f] = _json_safe(getattr(obj, f, None))

    confidence = {}
    extra = getattr(obj, "extra", None) or {}
    if isinstance(extra, dict):
        confidence = extra.get("field_confidence") or {}

    images = getattr(obj, "images", None)

    return {
        "id": obj.id,
        "target_type": target_type,
        "version": int(obj.version or 0),
        "owner_userid": obj.owner_userid,
        "raw_text": obj.raw_text,
        "description": getattr(obj, "description", None),
        "extracted_fields": extracted,
        "field_confidence": confidence,
        "risk_level": risk,
        "trigger_rules": triggers,
        "submitter_history": _submitter_history(db, obj.owner_userid),
        "locked_by": holder,
        "audit_status": obj.audit_status,
        "audit_reason": obj.audit_reason,
        "audited_by": obj.audited_by,
        "audited_at": _json_safe(obj.audited_at),
        "created_at": _json_safe(obj.created_at),
        "expires_at": _json_safe(getattr(obj, "expires_at", None)),
        "images": images,
    }


# ---------------------------------------------------------------------------
# 软锁
# ---------------------------------------------------------------------------

def lock(target_type: str, target_id: int, operator: str) -> None:
    _model_for(target_type)  # 参数校验

    # 先看当前持有者；若已是自己则只续 TTL（重入）。
    holder = get_audit_lock_holder(target_type, target_id)
    if holder == operator:
        refresh_audit_lock(target_type, target_id, operator)
        return
    if holder:
        raise BusinessException(40901, "条目正在被其他审核员处理", {"locked_by": holder})

    # 以 SETNX 的真实返回值为准：失败说明并发抢锁输了，读取最新 holder 并抛 40901。
    acquired = acquire_audit_lock(target_type, target_id, operator)
    if not acquired:
        real_holder = get_audit_lock_holder(target_type, target_id)
        raise BusinessException(
            40901, "条目正在被其他审核员处理",
            {"locked_by": real_holder} if real_holder else None,
        )


def unlock(target_type: str, target_id: int, operator: str) -> bool:
    _model_for(target_type)
    return release_audit_lock(target_type, target_id, operator)


# ---------------------------------------------------------------------------
# 通过 / 驳回 / 编辑 / Undo
# ---------------------------------------------------------------------------

def _atomic_version_update(
    db: Session,
    target_type: str,
    target_id: int,
    expected_version: int,
    updates: dict[str, Any],
):
    """按 `WHERE id=? AND version=?` 做原子 UPDATE + 版本递增。

    - rowcount==0 时抛 40902（乐观锁失败）
    - 成功后 refresh 附着的 ORM 对象以读回最新字段（含 updated_at 等 server onupdate）
    - 返回已刷新的 ORM 实例
    """
    model, _, _ = _model_for(target_type)
    new_version = int(expected_version) + 1
    patch = {**updates, "version": new_version}
    rowcount = (
        db.query(model)
        .filter(model.id == target_id, model.version == expected_version)
        .update(patch, synchronize_session=False)
    )
    if rowcount == 0:
        db.rollback()
        current = db.query(model).populate_existing().filter(model.id == target_id).first()
        raise BusinessException(
            40902, "此条目已被修改，请刷新",
            {"current_version": int(current.version) if current else 0},
        )
    # populate_existing 让 identity map 的对象按 DB 新值重新加载，
    # 否则 synchronize_session=False + 命中 identity map 会返回过期 ORM 实例，
    # 导致 after 快照和 audit_log 记录失真。
    obj = db.query(model).populate_existing().filter(model.id == target_id).first()
    return obj


def pass_action(db: Session, target_type: str, target_id: int, version: int, operator: str) -> None:
    obj = _load(db, target_type, target_id)
    _check_version(obj, version)  # 早退快速失败，仍非最终保障
    before = _snapshot(obj, target_type)

    obj = _atomic_version_update(
        db, target_type, target_id, version,
        {
            "audit_status": "passed",
            "audited_by": operator,
            "audited_at": datetime.now(),
        },
    )

    after = _snapshot(obj, target_type)
    write_admin_log(
        db,
        target_type=target_type, target_id=target_id,
        action="manual_pass", operator=operator,
        before=before, after=after,
    )
    db.commit()
    save_undo(target_type, target_id, {
        "action": "pass", "operator": operator,
        "before": before, "after": after,
        "ts": time.time(),
    })


def reject_action(
    db: Session,
    target_type: str,
    target_id: int,
    version: int,
    reason: str,
    operator: str,
    notify: bool = False,
    block_user: bool = False,
) -> None:
    obj = _load(db, target_type, target_id)
    _check_version(obj, version)
    before = _snapshot(obj, target_type)
    owner_userid = obj.owner_userid  # 记录 owner 便于后续封禁（原 obj 会被 refresh）

    obj = _atomic_version_update(
        db, target_type, target_id, version,
        {
            "audit_status": "rejected",
            "audit_reason": reason,
            "audited_by": operator,
            "audited_at": datetime.now(),
        },
    )

    after = _snapshot(obj, target_type)
    write_admin_log(
        db,
        target_type=target_type, target_id=target_id,
        action="manual_reject", operator=operator,
        before=before, after=after, reason=reason,
    )

    # 可选：同时封禁提交者
    if block_user:
        user = db.query(User).filter(User.external_userid == owner_userid).first()
        if user and user.status != "blocked":
            user_before = {"status": user.status, "blocked_reason": user.blocked_reason}
            user.status = "blocked"
            user.blocked_reason = reason
            write_admin_log(
                db,
                target_type="user", target_id=user.external_userid,
                action="manual_reject", operator=operator,
                before=user_before,
                after={"status": user.status, "blocked_reason": user.blocked_reason},
                reason=f"reject+block: {reason}",
            )

    db.commit()
    save_undo(target_type, target_id, {
        "action": "reject", "operator": operator,
        "before": before, "after": after,
        "ts": time.time(),
    })


def edit_action(
    db: Session,
    target_type: str,
    target_id: int,
    version: int,
    fields: dict[str, Any],
    operator: str,
) -> None:
    _, allowed, _ = _model_for(target_type)
    unknown = [f for f in fields.keys() if f not in allowed]
    if unknown:
        raise BusinessException(40101, f"不允许编辑的字段: {','.join(unknown)}")

    obj = _load(db, target_type, target_id)
    _check_version(obj, version)
    before = _snapshot(obj, target_type)

    obj = _atomic_version_update(db, target_type, target_id, version, dict(fields))
    after = _snapshot(obj, target_type)
    write_admin_log(
        db,
        target_type=target_type, target_id=target_id,
        action="manual_edit", operator=operator,
        before=before, after=after,
    )
    db.commit()
    save_undo(target_type, target_id, {
        "action": "edit", "operator": operator,
        "before": before, "after": after,
        "ts": time.time(),
    })


_DATETIME_COLS = {"audited_at", "expires_at", "deleted_at", "available_from"}


def _restore_value(column_name: str, value: Any) -> Any:
    """Undo 恢复时按列类型反序列化 snapshot 中的值。

    snapshot 在存入 Redis 前经 `_json_safe` 把 datetime 转成 ISO 字符串，
    这里再把它转回 Python datetime，避免写回 DateTime 列时出类型错误。
    """
    if value is None:
        return None
    if column_name in _DATETIME_COLS and isinstance(value, str):
        try:
            # available_from 是 date
            if column_name == "available_from" and len(value) == 10:
                return date.fromisoformat(value)
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return value


def undo(db: Session, target_type: str, target_id: int, operator: str) -> None:
    payload = pop_undo(target_type, target_id)
    if not payload:
        raise BusinessException(40903, "撤销窗口已过期（30 秒）")

    obj = _load(db, target_type, target_id)
    before_snapshot = payload.get("before") or {}
    _, allowed, snap_fields = _model_for(target_type)

    current_snapshot = _snapshot(obj, target_type)
    # 只恢复白名单字段 + audit_status + audit_reason / audited_by / audited_at / expires_at / delist_reason
    restorable_fields = snap_fields | {"audit_reason", "audited_by", "audited_at"}
    for key in restorable_fields:
        if key == "version":
            continue
        if key in before_snapshot:
            setattr(obj, key, _restore_value(key, before_snapshot.get(key)))
    # version += 1（表示这是一次新操作）
    obj.version = int(obj.version or 0) + 1

    write_admin_log(
        db,
        target_type=target_type, target_id=target_id,
        action="undo", operator=operator,
        before=current_snapshot,
        after=_snapshot(obj, target_type),
        reason=f"undo previous action: {payload.get('action')}",
    )
    db.commit()
