"""Job replacement integration checks that require a real MySQL database."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.core.exceptions import BusinessException
from app.config import settings
from app.db import SessionLocal, engine
from app.models import Job, JobReplacement, TargetCleanupTask, User
from app.services.job_admin_service import restore
from app.services.job_replace_service import cancel_candidate, create_replacement_candidate


pytestmark = pytest.mark.integration


def _active_job(owner_userid: str, now: datetime) -> Job:
    return Job(
        owner_userid=owner_userid,
        city="RR",
        job_category="RR",
        salary_floor_monthly=5000,
        pay_type="月薪",
        headcount=1,
        raw_text="repeatable read old job",
        audit_status="passed",
        activated_at=now,
        expires_at=now + timedelta(days=30),
        candidate_expires_at=None,
        version=1,
    )


def _create_candidate(
    db,
    old_job: Job,
    *,
    operation_id: str,
    source_msg_id: str,
    audit_status: str,
):
    return create_replacement_candidate(
        db,
        owner_userid=old_job.owner_userid,
        target_job_id=old_job.id,
        expected_version=old_job.version,
        operation_id=operation_id,
        source_msg_id=source_msg_id,
        complete_data={
            "city": "RR",
            "job_category": "RR",
            "salary_floor_monthly": 6000,
            "pay_type": "月薪",
            "headcount": 2,
        },
        raw_text="repeatable read candidate",
        media_ids=[],
        audit_result=SimpleNamespace(status=audit_status, reason=""),
    )


def _cleanup_replacement_rows(owner_userid: str) -> None:
    db = SessionLocal()
    try:
        job_ids = [
            row[0]
            for row in db.query(Job.id).filter(Job.owner_userid == owner_userid).all()
        ]
        if job_ids:
            db.query(TargetCleanupTask).filter(
                TargetCleanupTask.target_type == "job",
                TargetCleanupTask.target_id.in_(job_ids),
            ).delete(synchronize_session=False)
        db.query(JobReplacement).filter(
            JobReplacement.owner_userid == owner_userid,
        ).delete(synchronize_session=False)
        db.query(Job).filter(Job.owner_userid == owner_userid).delete(
            synchronize_session=False,
        )
        db.query(User).filter(User.external_userid == owner_userid).delete(
            synchronize_session=False,
        )
        db.commit()
    finally:
        db.rollback()
        db.close()


def test_replacement_persists_explicit_full_update_fields_on_mysql(monkeypatch):
    monkeypatch.setattr(settings, "job_replacement_enabled", True)
    owner_userid = f"replace-full-fields-{uuid4().hex}"
    db = SessionLocal()
    try:
        db.add(User(external_userid=owner_userid, role="factory"))
        db.flush()
        old_job = _active_job(
            owner_userid,
            datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0),
        )
        db.add(old_job)
        db.commit()

        _, candidate = create_replacement_candidate(
            db,
            owner_userid=owner_userid,
            target_job_id=old_job.id,
            expected_version=old_job.version,
            operation_id=str(uuid4()),
            source_msg_id=f"msg-{uuid4()}",
            complete_data={
                "city": "苏州市",
                "job_category": "电子厂",
                "salary_floor_monthly": 6800,
                "pay_type": "月薪",
                "headcount": 55,
                "address": "星湖街88号",
                "accept_couple": True,
                "employment_type": "厂家直招",
                "contract_type": "长期合同",
            },
            raw_text="完整的新岗位",
            media_ids=[],
            audit_result=SimpleNamespace(status="pending", reason=""),
        )
        candidate_id = candidate.id
        db.commit()
        db.close()

        verifier = SessionLocal()
        try:
            stored = verifier.get(Job, candidate_id)
            assert stored is not None
            assert stored.address == "星湖街88号"
            assert bool(stored.accept_couple) is True
            assert stored.employment_type == "厂家直招"
            assert stored.contract_type == "长期合同"
        finally:
            verifier.close()
    finally:
        if db.is_active:
            db.rollback()
        db.close()
        _cleanup_replacement_rows(owner_userid)


def test_cancel_reason_limit_prevents_mysql_truncation():
    db = SessionLocal()
    prefix = f"replace-limit-{uuid4().hex}"
    accepted_reason = "r" * 64
    rejected_reason = "r" * 65

    try:
        sql_mode = str(db.execute(text("SELECT @@SESSION.sql_mode")).scalar_one())
        assert "STRICT_TRANS_TABLES" in sql_mode or "STRICT_ALL_TABLES" in sql_mode
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        owner = User(external_userid=prefix, role="factory")
        old_job = Job(
            owner_userid=prefix,
            city="O02",
            job_category="O02",
            salary_floor_monthly=5000,
            pay_type="月薪",
            headcount=1,
            raw_text="old job",
            audit_status="passed",
            activated_at=now,
            expires_at=now + timedelta(days=30),
        )
        candidate = Job(
            owner_userid=prefix,
            city="O02",
            job_category="O02",
            salary_floor_monthly=5000,
            pay_type="月薪",
            headcount=1,
            raw_text="replacement candidate",
            audit_status="pending",
            candidate_expires_at=now + timedelta(days=1),
        )
        db.add(owner)
        db.flush()
        db.add_all([old_job, candidate])
        db.flush()

        relation = JobReplacement(
            operation_id=str(uuid4()),
            source_msg_id=f"msg-{uuid4()}",
            owner_userid=prefix,
            old_job_id=old_job.id,
            new_job_id=candidate.id,
            old_job_version=old_job.version,
            old_expires_at=old_job.expires_at,
            old_business_digest="0" * 64,
            old_business_digest_version=1,
            review_outcome="pending",
            lifecycle_status="awaiting_review",
            active_old_job_id=old_job.id,
        )
        db.add(relation)
        db.flush()

        with pytest.raises(BusinessException, match="replacement_cancel_reason_invalid"):
            cancel_candidate(
                db,
                relation.id,
                operator="integration-reviewer",
                reason=rejected_reason,
            )
        assert relation.lifecycle_status == "awaiting_review"
        assert relation.closed_reason is None
        assert candidate.deleted_at is None

        cancel_candidate(
            db,
            relation.id,
            operator="integration-reviewer",
            reason=accepted_reason,
        )
        db.flush()

        stored_reason = db.execute(
            text("SELECT closed_reason FROM job_replacement WHERE id=:id"),
            {"id": relation.id},
        ).scalar_one()
        assert stored_reason == accepted_reason
    finally:
        db.rollback()
        db.close()


def test_restore_blocks_replaced_and_deleted_jobs_on_mysql():
    connection = engine.connect()
    transaction = connection.begin()
    db = Session(bind=connection, expire_on_commit=False)
    prefix = f"restore-state-{uuid4().hex}"
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    commit_events = []

    def record_commit(session):
        commit_events.append(session)

    event.listen(db, "after_commit", record_commit)

    def job(**overrides):
        values = {
            "owner_userid": prefix,
            "city": "O04",
            "job_category": "O04",
            "salary_floor_monthly": 5000,
            "pay_type": "月薪",
            "headcount": 1,
            "raw_text": "restore integration",
            "audit_status": "passed",
            "activated_at": now,
            "expires_at": now + timedelta(days=10),
            "candidate_expires_at": None,
            "version": 1,
        }
        values.update(overrides)
        return Job(**values)

    try:
        db.add(User(external_userid=prefix, role="factory"))
        db.flush()
        ordinary = job(delist_reason="manual_delist")
        replaced = job(delist_reason="replaced")
        deleted = job(delist_reason="manual_delist", deleted_at=now)
        db.add_all([ordinary, replaced, deleted])
        db.flush()
        cleanup = TargetCleanupTask(
            operation_id=str(uuid4()),
            target_type="job",
            target_id=deleted.id,
            reason="manual_delist",
            reason_history=["manual_delist"],
            status="retry_wait",
        )
        db.add(cleanup)
        db.flush()

        for blocked in (replaced, deleted):
            with pytest.raises(BusinessException, match="job_not_restorable"):
                restore(db, blocked.id, blocked.version, "integration-reviewer")
        assert commit_events == []

        restore(db, ordinary.id, ordinary.version, "integration-reviewer")

        assert commit_events == [db]
        ordinary_id = ordinary.id
        replaced_id = replaced.id
        deleted_id = deleted.id
        cleanup_id = cleanup.id
        db.expire_all()
        ordinary = db.get(Job, ordinary_id)
        replaced = db.get(Job, replaced_id)
        deleted = db.get(Job, deleted_id)
        cleanup = db.get(TargetCleanupTask, cleanup_id)
        assert ordinary.delist_reason is None
        assert ordinary.deleted_at is None
        assert replaced.delist_reason == "replaced"
        assert deleted.delist_reason == "manual_delist"
        assert deleted.deleted_at == now
        assert cleanup.status == "retry_wait"
        assert transaction.is_active
    finally:
        event.remove(db, "after_commit", record_commit)
        db.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()


@pytest.mark.parametrize("repeated_identity", ["operation", "source_message"])
def test_rr_idempotency_recheck_finds_auto_activated_relation(
    monkeypatch,
    repeated_identity,
):
    monkeypatch.setattr(settings, "job_replacement_enabled", True)
    owner_userid = f"rr-idempotency-{uuid4().hex}"
    operation_id = str(uuid4())
    source_msg_id = f"msg-{uuid4()}"
    setup = SessionLocal()
    stale = None
    creator = None
    try:
        setup.add(User(external_userid=owner_userid, role="factory"))
        setup.flush()
        old_job = _active_job(
            owner_userid,
            datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0),
        )
        setup.add(old_job)
        setup.commit()
        old_job_id = old_job.id
        original_version = old_job.version
        setup.close()

        stale = SessionLocal()
        isolation = str(
            stale.execute(text("SELECT @@transaction_isolation")).scalar_one()
        ).upper()
        assert isolation == "REPEATABLE-READ"
        stale_old = stale.query(Job).filter(Job.id == old_job_id).one()
        assert stale.query(JobReplacement).filter(
            JobReplacement.old_job_id == old_job_id,
        ).first() is None

        creator = SessionLocal()
        creator_old = creator.query(Job).filter(Job.id == old_job_id).one()
        first_relation, first_candidate = _create_candidate(
            creator,
            creator_old,
            operation_id=operation_id,
            source_msg_id=source_msg_id,
            audit_status="passed",
        )
        creator.commit()
        first_relation_id = first_relation.id
        first_candidate_id = first_candidate.id
        creator.close()
        creator = None

        retry_operation = (
            operation_id if repeated_identity == "operation" else str(uuid4())
        )
        retry_source = (
            source_msg_id
            if repeated_identity == "source_message"
            else f"msg-{uuid4()}"
        )
        returned_relation, returned_candidate = create_replacement_candidate(
            stale,
            owner_userid=owner_userid,
            target_job_id=old_job_id,
            expected_version=original_version,
            operation_id=retry_operation,
            source_msg_id=retry_source,
            complete_data={
                "city": "unused",
                "job_category": "unused",
                "salary_floor_monthly": 1,
                "pay_type": "月薪",
                "headcount": 1,
            },
            raw_text="idempotent retry",
            media_ids=[],
            audit_result=SimpleNamespace(status="pending", reason=""),
        )

        assert returned_relation.id == first_relation_id
        assert returned_relation.lifecycle_status == "activated"
        assert returned_candidate.id == first_candidate_id
        assert returned_candidate.audit_status == "passed"
        assert stale_old.delist_reason == "replaced"
        assert stale_old.deleted_at is not None
        assert stale_old.version > original_version
        verifier = SessionLocal()
        try:
            assert verifier.query(JobReplacement).filter(
                JobReplacement.old_job_id == old_job_id,
            ).count() == 1
        finally:
            verifier.close()
    finally:
        if creator is not None:
            creator.rollback()
            creator.close()
        if stale is not None:
            stale.rollback()
            stale.close()
        if setup.is_active:
            setup.rollback()
            setup.close()
        _cleanup_replacement_rows(owner_userid)


def test_rr_active_relation_recheck_blocks_second_creation(monkeypatch):
    monkeypatch.setattr(settings, "job_replacement_enabled", True)
    owner_userid = f"rr-active-{uuid4().hex}"
    setup = SessionLocal()
    stale = None
    creator = None
    try:
        setup.add(User(external_userid=owner_userid, role="factory"))
        setup.flush()
        old_job = _active_job(
            owner_userid,
            datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0),
        )
        setup.add(old_job)
        setup.commit()
        old_job_id = old_job.id
        original_version = old_job.version
        setup.close()

        stale = SessionLocal()
        stale_old = stale.query(Job).filter(Job.id == old_job_id).one()
        assert stale.query(JobReplacement).filter(
            JobReplacement.old_job_id == old_job_id,
        ).first() is None

        creator = SessionLocal()
        creator_old = creator.query(Job).filter(Job.id == old_job_id).one()
        first_relation, _ = _create_candidate(
            creator,
            creator_old,
            operation_id=str(uuid4()),
            source_msg_id=f"msg-{uuid4()}",
            audit_status="pending",
        )
        creator.commit()
        first_relation_id = first_relation.id
        creator.close()
        creator = None

        with pytest.raises(BusinessException, match="replacement_in_progress"):
            _create_candidate(
                stale,
                stale_old,
                operation_id=str(uuid4()),
                source_msg_id=f"msg-{uuid4()}",
                audit_status="pending",
            )
        stale.rollback()

        verifier = SessionLocal()
        try:
            relations = verifier.query(JobReplacement).filter(
                JobReplacement.old_job_id == old_job_id,
            ).all()
            assert [relation.id for relation in relations] == [first_relation_id]
            assert relations[0].active_old_job_id == old_job_id
            assert relations[0].lifecycle_status == "awaiting_review"
            assert verifier.query(Job).filter(Job.owner_userid == owner_userid).count() == 2
            assert original_version < verifier.get(Job, old_job_id).version
        finally:
            verifier.close()
    finally:
        if creator is not None:
            creator.rollback()
            creator.close()
        if stale is not None:
            stale.rollback()
            stale.close()
        if setup.is_active:
            setup.rollback()
            setup.close()
        _cleanup_replacement_rows(owner_userid)
