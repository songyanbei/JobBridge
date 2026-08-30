"""Create a complete resume replacement candidate; activation belongs to phase 4."""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import event
from sqlalchemy.orm import Session, SessionTransaction

from app.core.exceptions import BusinessException
from app.core.logging_setup import identifier_hash
from app.models import Resume, ResumeReplacement
from app.services.admin_log_service import write_admin_log
from app.services.job_media_service import attach_media, mark_resume_media_delete_pending
from app.services.resume_activation_service import activate_resume
from app.services.lifecycle_config_service import get_resume_candidate_ttl_days
from app.services.resume_business_digest_service import DIGEST_VERSION, business_digest
from app.services.resume_mutation_service import (
    increment_resume_version, to_utc_naive, utc_now_naive,
)
from app.services.resume_replacement_lock_service import lock_replacement_creation, lock_replacement_graph
from app.services.target_cleanup_service import ensure_target_cleanup_task
from app.tasks.common import log_event

_PENDING_EVENTS = "resume_replace_pending_events"
logger = logging.getLogger(__name__)
REPLACEMENT_CANCEL_REASON_MAX_LENGTH = 64


def _contains(transaction, ancestor):
    while transaction is not None:
        if transaction is ancestor:
            return True
        transaction = transaction.parent
    return False


@event.listens_for(Session, "after_commit")
def _emit_committed_events(db: Session) -> None:
    if db.in_nested_transaction():
        return
    for _, fields in db.info.pop(_PENDING_EVENTS, []):
        try:
            log_event("resume_replace_started", **fields)
        except Exception:
            logger.exception("resume_replace_started telemetry failed")


@event.listens_for(Session, "after_soft_rollback")
def _discard_rolled_back_events(db: Session, previous_transaction: SessionTransaction) -> None:
    pending = db.info.get(_PENDING_EVENTS, [])
    kept = [item for item in pending if not _contains(item[0], previous_transaction)]
    if kept:
        db.info[_PENDING_EVENTS] = kept
    else:
        db.info.pop(_PENDING_EVENTS, None)


@event.listens_for(Session, "after_transaction_end")
def _discard_abandoned_events(db: Session, transaction: SessionTransaction) -> None:
    if transaction.parent is None and not db.in_transaction():
        db.info.pop(_PENDING_EVENTS, None)


def _schedule_started(db, relation, candidate):
    transaction = db.get_nested_transaction() or db.get_transaction()
    if transaction is None:
        raise RuntimeError("resume_replacement_event_requires_transaction")
    db.info.setdefault(_PENDING_EVENTS, []).append((transaction, {
        "old_resume_id": relation.old_resume_id,
        "new_resume_id": candidate.id,
        "batch_id": relation.operation_id,
        "user_hash": identifier_hash(relation.owner_userid),
    }))

_COPY_FIELDS = (
    "expected_cities", "expected_job_categories", "salary_expect_floor_monthly",
    "gender", "age", "accept_long_term", "accept_short_term", "expected_districts",
    "height", "weight", "education", "work_experience", "accept_night_shift",
    "accept_standing_work", "accept_overtime", "accept_outside_province",
    "couple_seeking_together", "has_health_certificate", "ethnicity",
    "available_from", "has_tattoo", "taboo", "description", "miniprogram_url", "extra",
)


def _candidate(
    owner_userid: str, data: dict, raw_text: str, audit_result, expires_at,
    *, audited_at=None,
) -> Resume:
    values = {field: data.get(field) for field in _COPY_FIELDS if field in data}
    values.update(
        owner_userid=owner_userid,
        expected_cities=data["expected_cities"],
        expected_job_categories=data["expected_job_categories"],
        salary_expect_floor_monthly=data["salary_expect_floor_monthly"],
        gender=data.get("gender", "男"),
        age=data["age"],
        accept_long_term=data.get("accept_long_term", True),
        accept_short_term=data.get("accept_short_term", False),
        raw_text=raw_text,
        images=None,
        audit_status="pending" if audit_result.status == "passed" else audit_result.status,
        audit_reason=audit_result.reason or None,
        audited_by="system",
        audited_at=to_utc_naive(audited_at) if audited_at is not None else utc_now_naive(),
        activated_at=None,
        expires_at=None,
        candidate_expires_at=expires_at,
    )
    return Resume(**values)


def create_replacement_candidate(
    db: Session, *, owner_userid: str, target_resume_id: int, expected_version: int,
    operation_id: str, source_msg_id: str, complete_data: dict, raw_text: str,
    media_ids: list[int], audit_result,
) -> tuple[ResumeReplacement, Resume]:
    from app.config import settings

    if not settings.resume_replacement_enabled:
        raise BusinessException(40904, "resume_replacement_disabled")
    existing, old = lock_replacement_creation(
        db, owner_userid, target_resume_id, operation_id, source_msg_id,
    )
    if existing is not None:
        if not (existing.operation_id == operation_id or existing.source_msg_id == source_msg_id):
            raise BusinessException(40904, "replacement_in_progress")
        if existing.owner_userid != owner_userid or existing.old_resume_id != target_resume_id:
            raise BusinessException(40904, "replacement_idempotency_mismatch")
        candidate = db.query(Resume).filter(Resume.id == existing.new_resume_id).with_for_update().one_or_none()
        if candidate is None:
            raise BusinessException(40904, "replacement_graph_incomplete")
        return existing, candidate
    now = utc_now_naive()
    if old is None or old.owner_userid != owner_userid:
        raise BusinessException(40401, "未找到可更新简历")
    if (
        old.audit_status != "passed" or old.activated_at is None or old.expires_at is None
        or old.expires_at <= now or old.deleted_at is not None or old.delist_reason is not None
    ):
        raise BusinessException(40904, "简历当前不可更新，请重新上传完整简历")
    if int(old.version or 0) != int(expected_version):
        raise BusinessException(40902, "简历已发生变化，请重新发起更新")

    increment_resume_version(old)
    replacement_base_version = int(old.version)
    candidate = _candidate(
        owner_userid, complete_data, raw_text, audit_result,
        now + timedelta(days=get_resume_candidate_ttl_days(db)),
        audited_at=now,
    )
    db.add(candidate)
    db.flush()
    if int(candidate.id) <= int(old.id):
        raise RuntimeError("resume_auto_increment_invariant_violated")
    if media_ids:
        candidate.images = attach_media(
            db, media_ids, "resume", candidate.id, owner_userid=owner_userid,
        )
    rejected = audit_result.status == "rejected"
    relation = ResumeReplacement(
        operation_id=operation_id,
        source_msg_id=source_msg_id,
        owner_userid=owner_userid,
        old_resume_id=old.id,
        new_resume_id=candidate.id,
        old_resume_version=replacement_base_version,
        old_expires_at=old.expires_at,
        old_business_digest=business_digest(old),
        old_business_digest_version=DIGEST_VERSION,
        review_outcome=audit_result.status,
        reviewed_at=now if audit_result.status != "pending" else None,
        reviewed_by="system" if audit_result.status != "pending" else None,
        lifecycle_status="closed" if rejected else "awaiting_review",
        active_old_resume_id=None if rejected else old.id,
        closed_reason="rejected" if rejected else None,
    )
    db.add(relation)
    db.flush()
    if audit_result.status == "passed":
        activate_replacement_locked(
            db, relation, old, candidate,
            expected_old_version=replacement_base_version, now=now,
        )
    _schedule_started(db, relation, candidate)
    return relation, candidate


def _relation_state(relation: ResumeReplacement) -> dict:
    return {
        "id": relation.id,
        "review_outcome": relation.review_outcome,
        "lifecycle_status": relation.lifecycle_status,
        "old_resume_version": relation.old_resume_version,
        "conflict_reason": relation.conflict_reason,
        "closed_reason": relation.closed_reason,
    }


def _natural_expiry_only(relation: ResumeReplacement, old: Resume) -> bool:
    return bool(
        int(old.version or 0) == int(relation.old_resume_version) + 1
        and old.delist_reason == "expired"
        and old.deleted_at is not None
        and old.expires_at == relation.old_expires_at
        and business_digest(
            old, digest_version=int(relation.old_business_digest_version),
        ) == relation.old_business_digest
    )


def activate_replacement_locked(
    db: Session, relation: ResumeReplacement, old: Resume, new: Resume, *,
    expected_old_version: int, now=None,
) -> bool:
    if relation.review_outcome != "passed" or relation.lifecycle_status not in {
        "awaiting_review", "conflict",
    }:
        raise BusinessException(40904, "replacement_not_activatable")
    if not (
        relation.owner_userid == old.owner_userid == new.owner_userid
        and relation.old_resume_id == old.id
        and relation.new_resume_id == new.id
    ):
        raise BusinessException(40904, "replacement_graph_ownership_mismatch")
    moment = to_utc_naive(now) if now is not None else utc_now_naive()
    if not new.candidate_expires_at or new.candidate_expires_at <= moment:
        raise BusinessException(40904, "candidate_expired")
    exact = bool(
        int(old.version or 0) == int(expected_old_version)
        and old.deleted_at is None and old.delist_reason is None
        and old.expires_at == relation.old_expires_at
    )
    digest_ok = business_digest(
        old, digest_version=int(relation.old_business_digest_version),
    ) == relation.old_business_digest
    natural = _natural_expiry_only(relation, old)
    if not ((exact and digest_ok) or natural):
        relation.lifecycle_status = "conflict"
        relation.conflict_reason = "replacement_conflict"
        relation.active_old_resume_id = old.id
        return False
    if new.activated_at is not None or new.expires_at is not None:
        raise BusinessException(40904, "candidate_already_activated")

    activate_resume(db, new, now=moment)
    if not natural:
        old.deleted_at = moment
        old.delist_reason = "replaced"
        increment_resume_version(old)
    relation.lifecycle_status = "activated"
    relation.activated_at = moment
    relation.active_old_resume_id = None
    relation.conflict_reason = None
    ensure_target_cleanup_task(
        db, "resume", old.id, reason="replaced", operation_id=relation.operation_id,
    )
    db.flush()
    try:
        from app.services.domain_outbox_service import append_domain_event
        for target, event_type, tombstone in (
            (old, "resume.replaced", True), (candidate, "resume.updated", False),
        ):
            append_domain_event(
                db, aggregate_type="resume", aggregate_id=int(target.id),
                aggregate_version=int(getattr(target, "aggregate_version", None) or target.version),
                event_type=event_type,
                payload={"resume_id": int(target.id), "status": "replaced" if tombstone else "candidate", "reason": "replacement"},
                tombstone=tombstone,
            )
    except Exception:
        logger.exception("resume domain event append failed")
    return True


def retry_activation(
    db: Session, replacement_id: int, expected_old_version: int, *,
    operator: str, reason: str,
) -> bool:
    relation, _, graph = lock_replacement_graph(db, replacement_id)
    if relation is None or relation.review_outcome != "passed" or relation.lifecycle_status != "conflict":
        raise BusinessException(40904, "replacement_not_retryable")
    old, new = graph[relation.old_resume_id], graph[relation.new_resume_id]
    now = utc_now_naive()
    if not new.candidate_expires_at or new.candidate_expires_at <= now:
        raise BusinessException(40904, "candidate_expired")
    if int(old.version or 0) != int(expected_old_version):
        raise BusinessException(40902, "简历版本已变化", {"current_version": int(old.version or 0)})
    before = _relation_state(relation)
    relation.old_resume_version = int(old.version or 0)
    relation.old_expires_at = old.expires_at
    relation.old_business_digest_version = DIGEST_VERSION
    relation.old_business_digest = business_digest(old)
    relation.reviewed_by = operator
    relation.reviewed_at = now
    activated = activate_replacement_locked(
        db, relation, old, new, expected_old_version=expected_old_version, now=now,
    )
    write_admin_log(
        db, target_type="resume", target_id=new.id, action="manual_edit",
        operator=operator, before=before, after=_relation_state(relation),
        reason=f"replacement_activation_retry:{reason}"[:255],
    )
    return activated


def cancel_candidate(
    db: Session, replacement_id: int, *, operator: str,
    reason: str = "operator_cancelled",
) -> None:
    if not isinstance(reason, str) or not 1 <= len(reason) <= REPLACEMENT_CANCEL_REASON_MAX_LENGTH:
        raise BusinessException(40101, "replacement_cancel_reason_invalid")
    relation, _, graph = lock_replacement_graph(db, replacement_id)
    if relation is None or relation.lifecycle_status not in {"awaiting_review", "conflict"}:
        raise BusinessException(40904, "replacement_not_cancellable")
    candidate = graph[relation.new_resume_id]
    if not candidate.candidate_expires_at or candidate.candidate_expires_at <= utc_now_naive():
        raise BusinessException(40904, "candidate_expired")
    before = _relation_state(relation)
    now = utc_now_naive()
    relation.lifecycle_status = "closed"
    relation.closed_reason = reason
    relation.active_old_resume_id = None
    relation.candidate_cleaned_at = now
    relation.reviewed_by = operator
    relation.reviewed_at = now
    candidate.deleted_at = now
    candidate.delist_reason = "candidate_cancelled"
    increment_resume_version(candidate)
    mark_resume_media_delete_pending(db, candidate.id)
    ensure_target_cleanup_task(db, "resume", candidate.id, reason="candidate_cancelled")
    write_admin_log(
        db, target_type="resume", target_id=candidate.id, action="manual_reject",
        operator=operator, before=before, after=_relation_state(relation),
        reason=f"replacement_candidate_cancel:{reason}"[:255],
    )
