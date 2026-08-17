"""Create a complete resume replacement candidate; activation belongs to phase 4."""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import event
from sqlalchemy.orm import Session, SessionTransaction

from app.core.exceptions import BusinessException
from app.core.logging_setup import identifier_hash
from app.models import Resume, ResumeReplacement
from app.services.job_media_service import attach_media
from app.services.lifecycle_config_service import get_resume_candidate_ttl_days
from app.services.resume_business_digest_service import DIGEST_VERSION, business_digest
from app.services.resume_mutation_service import increment_resume_version, utc_now_naive
from app.services.resume_replacement_lock_service import lock_replacement_creation
from app.tasks.common import log_event

_PENDING_EVENTS = "resume_replace_pending_events"
logger = logging.getLogger(__name__)


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


def _candidate(owner_userid: str, data: dict, raw_text: str, audit_result, expires_at) -> Resume:
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
        audited_at=utc_now_naive(),
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
    candidate = _candidate(
        owner_userid, complete_data, raw_text, audit_result,
        now + timedelta(days=get_resume_candidate_ttl_days(db)),
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
        old_resume_version=int(old.version),
        old_expires_at=old.expires_at,
        old_business_digest=business_digest(old),
        old_business_digest_version=DIGEST_VERSION,
        review_outcome=audit_result.status,
        reviewed_at=now if rejected else None,
        reviewed_by="system" if rejected else None,
        lifecycle_status="closed" if rejected else "awaiting_review",
        active_old_resume_id=None if rejected else old.id,
        closed_reason="rejected" if rejected else None,
    )
    db.add(relation)
    db.flush()
    _schedule_started(db, relation, candidate)
    return relation, candidate
