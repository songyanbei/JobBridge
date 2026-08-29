"""Small, explicit admin operations for phase-11 cleanup recovery."""
from __future__ import annotations

from datetime import timedelta
import hashlib

from sqlalchemy.orm import Session

from app.core.exceptions import BusinessException
from app.models import (
    AdminUser, AuditLog, MediaAssetLifecycle, Resume, ResumeMediaIsolationIssue,
    TargetCleanupTask,
)
from app.services.admin_log_service import write_admin_log
from app.services.resume_mutation_service import increment_resume_version, utc_now_naive

MAX_REDRIVE_BATCH = 50
MAX_REDRIVE_BATCHES_PER_MINUTE = 2


def list_target_tasks(db: Session, *, status: str | None, limit: int) -> list[dict]:
    query = db.query(TargetCleanupTask)
    if status:
        query = query.filter(TargetCleanupTask.status == status)
    rows = query.order_by(TargetCleanupTask.id.desc()).limit(min(max(limit, 1), 100)).all()
    return [{
        "id": int(row.id), "target_type": row.target_type,
        "target_id": int(row.target_id), "status": row.status,
        "attempt_count": int(row.attempt_count or 0),
        "next_attempt_at": row.next_attempt_at,
        "lease_expires_at": row.lease_expires_at,
        "updated_at": row.updated_at,
    } for row in rows]


def list_media_issues(db: Session, *, status: str | None, limit: int) -> list[dict]:
    query = db.query(ResumeMediaIsolationIssue)
    if status:
        query = query.filter(ResumeMediaIsolationIssue.status == status)
    rows = query.order_by(ResumeMediaIsolationIssue.id.desc()).limit(min(max(limit, 1), 100)).all()
    return [{
        "id": int(row.id), "resume_id": int(row.resume_id) if row.resume_id else None,
        "issue_type": row.issue_type, "status": row.status,
        "disposition": row.disposition, "approved_by": row.approved_by,
        "approved_at": row.approved_at, "executed_by": row.executed_by,
        "executed_at": row.executed_at, "created_at": row.created_at,
    } for row in rows]


def list_media_dead_letters(db: Session, *, limit: int) -> list[dict]:
    """Return only operational metadata; object keys and owner data stay private."""
    rows = db.query(MediaAssetLifecycle).filter(
        MediaAssetLifecycle.state == "dead_letter",
    ).order_by(MediaAssetLifecycle.id.desc()).limit(min(max(limit, 1), 100)).all()
    return [{
        "id": int(row.id), "status": row.state,
        "attempt_count": int(row.attempt_count or 0),
        "next_attempt_at": row.next_attempt_at,
        "lease_expires_at": row.lease_expires_at,
        "updated_at": row.updated_at,
    } for row in rows]


def _enforce_redrive_rate(db: Session, operator: str) -> None:
    # Serialize the complete check -> mutation -> audit -> commit unit on the
    # authenticated administrator's durable row.  This is a MySQL current
    # read, so a waiter observes the preceding transaction's committed audit
    # before counting.  A rollback reverts both the work and its audit, hence
    # failed batches do not consume quota.  The row is released by the commit
    # at the end of ``redrive_dead_letters`` (or caller rollback/close).
    admin = db.query(AdminUser.id).filter(
        AdminUser.username == operator,
        AdminUser.enabled == 1,
    ).with_for_update().first()
    if admin is None:
        raise BusinessException(40301, "cleanup_operator_not_found")
    cutoff = utc_now_naive() - timedelta(minutes=1)
    count = db.query(AuditLog.id).filter(
        AuditLog.operator == operator,
        AuditLog.created_at >= cutoff,
        AuditLog.reason.like("cleanup_dead_letter_retry:%"),
    ).count()
    if count >= MAX_REDRIVE_BATCHES_PER_MINUTE:
        raise BusinessException(40904, "cleanup_redrive_rate_limited")


def redrive_dead_letters(
    db: Session, *, kind: str, ids: list[int], reason: str, operator: str,
) -> list[dict]:
    reason = str(reason).strip()
    if not 1 <= len(reason) <= 160 or any(ord(char) < 32 for char in reason):
        raise BusinessException(40101, "cleanup_redrive_reason_invalid")
    if kind not in {"target", "media"}:
        raise BusinessException(40101, "cleanup_kind_invalid")
    unique_ids = list(dict.fromkeys(ids))
    if not unique_ids or len(unique_ids) > MAX_REDRIVE_BATCH:
        raise BusinessException(40101, "cleanup_redrive_batch_invalid")
    reason_digest = hashlib.sha256(reason.encode("utf-8")).hexdigest()[:16]
    _enforce_redrive_rate(db, operator)
    now = utc_now_naive()
    results: list[dict] = []
    before_rows: list[dict] = []
    if kind == "target":
        rows = db.query(TargetCleanupTask).filter(
            TargetCleanupTask.id.in_(unique_ids)
        ).order_by(TargetCleanupTask.id).with_for_update().all()
        by_id = {int(row.id): row for row in rows}
        for item_id in unique_ids:
            row = by_id.get(item_id)
            if row is None:
                results.append({"id": item_id, "result": "not_found"})
            elif row.status != "dead_letter" or (
                row.lease_expires_at is not None and row.lease_expires_at > now
            ):
                results.append({"id": item_id, "result": "not_retryable"})
            else:
                before_rows.append({"id": item_id, "status": row.status,
                                    "attempt_count": int(row.attempt_count or 0)})
                row.status = "pending"
                row.next_attempt_at = now
                row.lease_owner = None
                row.lease_expires_at = None
                row.last_error = None
                results.append({"id": item_id, "result": "queued"})
    else:
        rows = db.query(MediaAssetLifecycle).filter(
            MediaAssetLifecycle.id.in_(unique_ids)
        ).order_by(MediaAssetLifecycle.id).with_for_update().all()
        by_id = {int(row.id): row for row in rows}
        for item_id in unique_ids:
            row = by_id.get(item_id)
            if row is None:
                results.append({"id": item_id, "result": "not_found"})
            elif row.state != "dead_letter" or (
                row.lease_expires_at is not None and row.lease_expires_at > now
            ):
                results.append({"id": item_id, "result": "not_retryable"})
            else:
                before_rows.append({"id": item_id, "status": row.state,
                                    "attempt_count": int(row.attempt_count or 0)})
                row.state = "delete_pending"
                row.next_attempt_at = now
                row.lease_owner = None
                row.lease_expires_at = None
                row.last_error = None
                results.append({"id": item_id, "result": "queued"})
    write_admin_log(
        db, target_type="system", target_id=f"cleanup:{kind}", action="manual_edit",
        operator=operator, before={"items": before_rows}, after={"items": results},
        reason=f"cleanup_dead_letter_retry:reason_sha256={reason_digest}",
    )
    db.commit()
    return results


def approve_media_issue(
    db: Session, *, issue_id: int, disposition: str, reason: str, operator: str,
) -> dict:
    if disposition not in {"assign_owner", "detach_reference", "delete_object"}:
        raise BusinessException(40101, "media_disposition_invalid")
    row = db.query(ResumeMediaIsolationIssue).filter(
        ResumeMediaIsolationIssue.id == issue_id
    ).with_for_update().first()
    if row is None:
        raise BusinessException(40401, "media_isolation_issue_not_found")
    if row.status != "open":
        raise BusinessException(40904, "media_isolation_issue_not_open")
    row.status = "approved"
    row.disposition = disposition
    row.approval_reason = reason
    row.approved_by = operator
    row.approved_at = utc_now_naive()
    write_admin_log(
        db, target_type="system", target_id=f"media-isolation:{issue_id}",
        action="manual_edit", operator=operator,
        before={"status": "open"}, after={"status": "approved", "disposition": disposition},
        reason="media_isolation_approved",
    )
    db.commit()
    return {"id": issue_id, "status": row.status, "disposition": row.disposition}


def execute_media_issue(db: Session, *, issue_id: int, operator: str) -> dict:
    row = db.query(ResumeMediaIsolationIssue).filter(
        ResumeMediaIsolationIssue.id == issue_id
    ).with_for_update().first()
    if row is None:
        raise BusinessException(40401, "media_isolation_issue_not_found")
    if row.status != "approved":
        raise BusinessException(40904, "media_isolation_issue_not_approved")
    if row.approved_by == operator:
        raise BusinessException(40301, "media_isolation_four_eyes_required")
    resume = None
    matching_keys: list[str] = []
    if row.resume_id is not None:
        resume = db.query(Resume).filter(Resume.id == row.resume_id).with_for_update().first()
        if resume is not None:
            matching_keys = [key for key in (resume.images or []) if isinstance(key, str)
                             and hashlib.sha256(key.encode("utf-8")).hexdigest() == row.key_hash]
    assets = db.query(MediaAssetLifecycle).order_by(MediaAssetLifecycle.id).with_for_update().all()
    matching_assets = [asset for asset in assets
                       if hashlib.sha256(asset.object_key.encode("utf-8")).hexdigest() == row.key_hash]
    if row.disposition == "assign_owner":
        if resume is None or not matching_assets:
            raise BusinessException(40904, "media_isolation_target_not_resolvable")
        for asset in matching_assets:
            asset.owner_userid = resume.owner_userid
            asset.entity_type = "resume"
            asset.entity_id = resume.id
            asset.state = "attached"
            asset.deleted_at = None
    elif row.disposition == "detach_reference":
        if resume is None or not matching_keys:
            raise BusinessException(40904, "media_isolation_target_not_resolvable")
        resume.images = [key for key in (resume.images or []) if key not in set(matching_keys)]
        increment_resume_version(resume)
    elif row.disposition == "delete_object":
        if not matching_assets:
            raise BusinessException(40904, "media_isolation_target_not_resolvable")
        for asset in matching_assets:
            asset.state = "delete_pending"
            asset.next_attempt_at = utc_now_naive()
            asset.lease_owner = None
            asset.lease_expires_at = None
    else:
        raise BusinessException(40904, "media_isolation_disposition_missing")
    now = utc_now_naive()
    row.status = "resolved"
    row.executed_by = operator
    row.executed_at = now
    row.resolved_at = now
    write_admin_log(
        db, target_type="system", target_id=f"media-isolation:{issue_id}",
        action="manual_edit", operator=operator,
        before={"status": "approved", "disposition": row.disposition},
        after={"status": "resolved", "disposition": row.disposition},
        reason="media_isolation_executed",
    )
    db.commit()
    return {"id": issue_id, "status": row.status, "disposition": row.disposition}
