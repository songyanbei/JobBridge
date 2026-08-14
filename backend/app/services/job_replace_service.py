"""Create complete replacement candidates and atomically activate approvals."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import event
from sqlalchemy.orm import Session, SessionTransaction

from app.core.exceptions import BusinessException
from app.core.logging_setup import identifier_hash
from app.models import Job, JobReplacement
from app.services.job_activation_service import activate_job
from app.services.admin_log_service import write_admin_log
from app.services.job_business_digest_service import DIGEST_VERSION, business_digest
from app.services.job_media_service import attach_media, mark_job_media_delete_pending
from app.services.job_mutation_service import increment_version, lock_job_for_mutation
from app.services.job_replacement_lock_service import (
    lock_replacement_creation,
    lock_replacement_graph,
)
from app.services.lifecycle_config_service import get_job_candidate_ttl_days
from app.services.target_cleanup_service import ensure_job_cleanup_task
from app.tasks.common import log_event

REPLACEMENT_CANCEL_REASON_MAX_LENGTH = 64
_PENDING_REPLACEMENT_EVENTS = "job_replace_pending_events"

logger = logging.getLogger(__name__)


def _transaction_contains(
    transaction: SessionTransaction | None,
    ancestor: SessionTransaction,
) -> bool:
    while transaction is not None:
        if transaction is ancestor:
            return True
        transaction = transaction.parent
    return False


@event.listens_for(Session, "after_commit")
def _emit_committed_replacement_events(db: Session) -> None:
    # SQLAlchemy also fires after_commit for a released savepoint.
    if db.in_nested_transaction():
        return
    pending = db.info.pop(_PENDING_REPLACEMENT_EVENTS, [])
    for _, fields in pending:
        try:
            log_event("job_replace_started", **fields)
        except Exception:
            logger.exception("job_replace_started telemetry failed")


@event.listens_for(Session, "after_soft_rollback")
def _discard_rolled_back_replacement_events(
    db: Session,
    previous_transaction: SessionTransaction,
) -> None:
    pending = db.info.get(_PENDING_REPLACEMENT_EVENTS, [])
    remaining = [
        item for item in pending
        if not _transaction_contains(item[0], previous_transaction)
    ]
    if remaining:
        db.info[_PENDING_REPLACEMENT_EVENTS] = remaining
    else:
        db.info.pop(_PENDING_REPLACEMENT_EVENTS, None)


@event.listens_for(Session, "after_transaction_end")
def _discard_abandoned_replacement_events(
    db: Session,
    transaction: SessionTransaction,
) -> None:
    if transaction.parent is None and not db.in_transaction():
        db.info.pop(_PENDING_REPLACEMENT_EVENTS, None)


def _emit_replacement_started_after_commit(
    db: Session,
    *,
    old_job_id: int,
    new_job_id: int,
    operation_id: str,
    owner_userid: str,
) -> None:
    transaction = db.get_nested_transaction() or db.get_transaction()
    if transaction is None:
        raise RuntimeError("replacement_event_requires_transaction")
    db.info.setdefault(_PENDING_REPLACEMENT_EVENTS, []).append((
        transaction,
        {
            "old_job_id": old_job_id,
            "new_job_id": new_job_id,
            "batch_id": operation_id,
            "user_hash": identifier_hash(owner_userid),
        },
    ))

_COPY_FIELDS = (
    "city", "district", "address", "job_category", "job_sub_category",
    "salary_floor_monthly", "salary_ceiling_monthly", "pay_type", "headcount",
    "gender_required", "age_min", "age_max", "is_long_term", "provide_meal",
    "provide_housing", "dorm_condition", "shift_pattern", "work_hours",
    "accept_couple", "accept_student", "accept_minority", "height_required",
    "experience_required", "education_required", "rebate", "employment_type",
    "contract_type", "min_duration", "description", "miniprogram_url", "extra",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _relation_state(relation: JobReplacement) -> dict:
    return {
        "id": relation.id,
        "review_outcome": relation.review_outcome,
        "lifecycle_status": relation.lifecycle_status,
        "old_job_version": relation.old_job_version,
        "conflict_reason": relation.conflict_reason,
        "closed_reason": relation.closed_reason,
    }


def _candidate_from_complete_data(
    owner_userid: str,
    data: dict,
    raw_text: str,
    audit_result,
    candidate_expires_at: datetime,
) -> Job:
    values = {field: data.get(field) for field in _COPY_FIELDS if field in data}
    values.update(
        owner_userid=owner_userid,
        city=data["city"],
        job_category=data["job_category"],
        salary_floor_monthly=data["salary_floor_monthly"],
        pay_type=data["pay_type"],
        headcount=data["headcount"],
        gender_required=data.get("gender_required", "不限"),
        is_long_term=data.get("is_long_term", True),
        raw_text=raw_text,
        images=None,
        audit_status="pending" if audit_result.status == "passed" else audit_result.status,
        audit_reason=audit_result.reason or None,
        audited_by="system",
        audited_at=_utcnow(),
        expires_at=None,
        activated_at=None,
        candidate_expires_at=candidate_expires_at,
    )
    return Job(**values)


def create_replacement_candidate(
    db: Session,
    *,
    owner_userid: str,
    target_job_id: int,
    expected_version: int,
    operation_id: str,
    source_msg_id: str,
    complete_data: dict,
    raw_text: str,
    media_ids: list[int],
    audit_result,
) -> tuple[JobReplacement, Job]:
    from app.config import settings

    if not settings.job_replacement_enabled:
        raise BusinessException(40904, "job_replacement_disabled")
    existing, old = lock_replacement_creation(db, target_job_id, operation_id, source_msg_id)
    if existing is not None:
        same_request = (
            existing.operation_id == operation_id
            or existing.source_msg_id == source_msg_id
        )
        if not same_request:
            raise BusinessException(40904, "replacement_in_progress")
        if existing.owner_userid != owner_userid or existing.old_job_id != target_job_id:
            raise BusinessException(40904, "replacement_idempotency_mismatch")
        candidate = lock_job_for_mutation(db, existing.new_job_id)
        if candidate is None:
            raise BusinessException(40904, "replacement_graph_incomplete")
        return existing, candidate
    if old is None or old.owner_userid != owner_userid:
        raise BusinessException(40401, "岗位不存在或无权更新")
    if old.audit_status != "passed" or old.deleted_at is not None or old.delist_reason is not None:
        raise BusinessException(40904, "岗位当前不可更新，请重新发布")
    if int(old.version or 0) != int(expected_version):
        raise BusinessException(40902, "岗位已发生变化，请重新发起更新", {
            "current_version": int(old.version or 0),
        })

    # Establishing a replacement changes the old Job's mutation boundary.
    # Keeping this increment under the old Job row lock makes any concurrent
    # optimistic admin write fail instead of silently editing the audit basis.
    increment_version(old)
    replacement_base_version = int(old.version)

    candidate_expiry = _utcnow() + timedelta(days=get_job_candidate_ttl_days(db))
    new_job = _candidate_from_complete_data(
        owner_userid, complete_data, raw_text, audit_result, candidate_expiry,
    )
    db.add(new_job)
    db.flush()
    if int(new_job.id) <= int(old.id):
        raise RuntimeError("job_auto_increment_invariant_violated")
    if media_ids:
        new_job.images = attach_media(
            db, media_ids, "job", new_job.id,
            owner_userid=owner_userid,
        )

    relation = JobReplacement(
        operation_id=operation_id,
        source_msg_id=source_msg_id,
        owner_userid=owner_userid,
        old_job_id=old.id,
        new_job_id=new_job.id,
        old_job_version=replacement_base_version,
        old_expires_at=old.expires_at,
        old_business_digest=business_digest(old),
        old_business_digest_version=DIGEST_VERSION,
        review_outcome=audit_result.status,
        reviewed_at=_utcnow() if audit_result.status != "pending" else None,
        reviewed_by="system" if audit_result.status != "pending" else None,
        lifecycle_status="closed" if audit_result.status == "rejected" else "awaiting_review",
        active_old_job_id=old.id if audit_result.status != "rejected" else None,
        closed_reason="rejected" if audit_result.status == "rejected" else None,
    )
    db.add(relation)
    db.flush()
    if audit_result.status == "passed":
        activate_replacement_locked(
            db,
            relation,
            old,
            new_job,
            expected_old_version=replacement_base_version,
        )
    _emit_replacement_started_after_commit(
        db,
        old_job_id=old.id,
        new_job_id=new_job.id,
        operation_id=operation_id,
        owner_userid=owner_userid,
    )
    return relation, new_job


def _natural_expiry_only(relation: JobReplacement, old: Job) -> bool:
    return (
        int(old.version or 0) == int(relation.old_job_version) + 1
        and old.delist_reason == "expired"
        and old.deleted_at is not None
        and old.expires_at == relation.old_expires_at
        and business_digest(
            old, digest_version=int(relation.old_business_digest_version)
        ) == relation.old_business_digest
    )


def activate_replacement_locked(
    db: Session,
    relation: JobReplacement,
    old: Job,
    new: Job,
    *,
    expected_old_version: int,
) -> bool:
    if relation.review_outcome != "passed" or relation.lifecycle_status not in {"awaiting_review", "conflict"}:
        raise BusinessException(40904, "replacement_not_activatable")
    if not (
        relation.owner_userid == old.owner_userid == new.owner_userid
        and relation.old_job_id == old.id
        and relation.new_job_id == new.id
    ):
        raise BusinessException(40904, "replacement_graph_ownership_mismatch")
    if not new.candidate_expires_at or new.candidate_expires_at <= _utcnow():
        raise BusinessException(40904, "candidate_expired")
    exact = (
        int(old.version or 0) == int(expected_old_version)
        and old.deleted_at is None
        and old.delist_reason is None
    )
    digest_ok = business_digest(
        old, digest_version=int(relation.old_business_digest_version)
    ) == relation.old_business_digest
    natural = _natural_expiry_only(relation, old)
    if not ((exact and digest_ok) or natural):
        relation.lifecycle_status = "conflict"
        relation.conflict_reason = "old_job_changed"
        relation.active_old_job_id = old.id
        return False
    if new.audit_status == "passed" or new.expires_at is not None:
        raise BusinessException(40904, "candidate_already_activated")

    activate_job(db, new)
    if not natural:
        old.deleted_at = new.activated_at
        old.delist_reason = "replaced"
        increment_version(old)
    relation.lifecycle_status = "activated"
    relation.activated_at = new.activated_at
    relation.active_old_job_id = None
    relation.conflict_reason = None
    ensure_job_cleanup_task(db, old.id, reason="replaced", operation_id=relation.operation_id)
    db.flush()
    return True


def activate_replacement(
    db: Session,
    replacement_id: int,
    expected_old_version: int,
    *,
    operator: str | None = None,
) -> bool:
    relation, _, by_id = lock_replacement_graph(db, replacement_id)
    if relation is None:
        raise BusinessException(40401, "replacement 不存在")
    if operator:
        relation.reviewed_by = operator
        relation.reviewed_at = _utcnow()
    return activate_replacement_locked(
        db,
        relation,
        by_id[relation.old_job_id],
        by_id[relation.new_job_id],
        expected_old_version=expected_old_version,
    )


def retry_activation(
    db: Session,
    replacement_id: int,
    expected_old_version: int,
    *,
    operator: str,
    reason: str,
) -> bool:
    relation, _, by_id = lock_replacement_graph(db, replacement_id)
    if relation is None or relation.review_outcome != "passed" or relation.lifecycle_status != "conflict":
        raise BusinessException(40904, "replacement_not_retryable")
    old, new = by_id[relation.old_job_id], by_id[relation.new_job_id]
    before = _relation_state(relation)
    if not new.candidate_expires_at or new.candidate_expires_at <= _utcnow():
        raise BusinessException(40904, "candidate_expired")
    if int(old.version or 0) != int(expected_old_version):
        raise BusinessException(40902, "岗位版本已变化", {"current_version": int(old.version or 0)})
    relation.old_job_version = old.version
    relation.old_expires_at = old.expires_at
    relation.old_business_digest_version = DIGEST_VERSION
    relation.old_business_digest = business_digest(old)
    relation.conflict_reason = f"retry:{reason}"[:255]
    relation.reviewed_by = operator
    relation.reviewed_at = _utcnow()
    activated = activate_replacement_locked(
        db, relation, old, new, expected_old_version=expected_old_version,
    )
    write_admin_log(
        db,
        target_type="job",
        target_id=new.id,
        action="manual_edit",
        operator=operator,
        before=before,
        after=_relation_state(relation),
        reason=f"replacement_activation_retry:{reason}"[:255],
    )
    return activated


def cancel_candidate(
    db: Session,
    replacement_id: int,
    *,
    operator: str,
    reason: str = "operator_cancelled",
) -> None:
    if not isinstance(reason, str) or not 1 <= len(reason) <= REPLACEMENT_CANCEL_REASON_MAX_LENGTH:
        raise BusinessException(40101, "replacement_cancel_reason_invalid")
    relation, _, by_id = lock_replacement_graph(db, replacement_id)
    if relation is None or relation.lifecycle_status not in {"awaiting_review", "conflict"}:
        raise BusinessException(40904, "replacement_not_cancellable")
    candidate = by_id[relation.new_job_id]
    before = _relation_state(relation)
    now = _utcnow()
    relation.lifecycle_status = "closed"
    relation.closed_reason = reason
    relation.active_old_job_id = None
    relation.candidate_cleaned_at = now
    relation.reviewed_by = operator
    relation.reviewed_at = now
    candidate.deleted_at = now
    increment_version(candidate)
    mark_job_media_delete_pending(db, candidate.id, include_pending=True)
    ensure_job_cleanup_task(db, candidate.id, reason="candidate_cancelled")
    write_admin_log(
        db,
        target_type="job",
        target_id=candidate.id,
        action="manual_reject",
        operator=operator,
        before=before,
        after=_relation_state(relation),
        reason=f"replacement_candidate_cancel:{reason}"[:255],
    )
