"""Small real-MySQL gate for the stage-4 replacement transaction.

The two ordering tests coordinate with Events after row locks are acquired; no
timing sleeps are used.  Expiry is represented by its stage-4 row mutation only
because the periodic worker belongs to stage 5.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Event
from uuid import uuid4

import pytest

from app.db import SessionLocal
from app.models import Resume, ResumeReplacement, TargetCleanupTask, User
from app.services import resume_replace_service
from app.services.resume_business_digest_service import business_digest
from app.services.resume_replacement_lock_service import lock_replacement_graph


pytestmark = pytest.mark.integration


def _setup_graph(owner: str):
    db = SessionLocal()
    now = datetime.utcnow().replace(microsecond=0)
    try:
        db.add(User(external_userid=owner, role="worker"))
        db.flush()
        old = Resume(
            owner_userid=owner, expected_cities=["苏州"],
            expected_job_categories=["普工"], salary_expect_floor_monthly=5000,
            gender="男", age=30, accept_long_term=True, accept_short_term=False,
            raw_text="stage4 old", audit_status="passed", activated_at=now - timedelta(days=30),
            expires_at=now, candidate_expires_at=None, version=2,
        )
        new = Resume(
            owner_userid=owner, expected_cities=["苏州"],
            expected_job_categories=["普工"], salary_expect_floor_monthly=6000,
            gender="男", age=31, accept_long_term=True, accept_short_term=False,
            raw_text="stage4 candidate", audit_status="pending", activated_at=None,
            expires_at=None, candidate_expires_at=now + timedelta(days=7), version=1,
        )
        db.add_all([old, new])
        db.flush()
        db.refresh(old)
        relation = ResumeReplacement(
            operation_id=str(uuid4()), source_msg_id=f"stage4-{uuid4()}", owner_userid=owner,
            old_resume_id=old.id, new_resume_id=new.id, old_resume_version=old.version,
            old_expires_at=old.expires_at, old_business_digest=business_digest(old),
            old_business_digest_version=1, review_outcome="pending",
            lifecycle_status="awaiting_review", active_old_resume_id=old.id,
        )
        db.add(relation)
        db.commit()
        return relation.id, old.id, new.id
    finally:
        db.close()


def _cleanup(owner: str):
    db = SessionLocal()
    try:
        ids = [row[0] for row in db.query(Resume.id).filter(Resume.owner_userid == owner).all()]
        if ids:
            db.query(TargetCleanupTask).filter(
                TargetCleanupTask.target_type == "resume",
                TargetCleanupTask.target_id.in_(ids),
            ).delete(synchronize_session=False)
        db.query(ResumeReplacement).filter_by(owner_userid=owner).delete(synchronize_session=False)
        db.query(Resume).filter_by(owner_userid=owner).delete(synchronize_session=False)
        db.query(User).filter_by(external_userid=owner).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def test_expiry_lock_first_then_review_uses_only_base_plus_one_exception():
    owner = f"stage4-expiry-first-{uuid4().hex}"
    relation_id, old_id, new_id = _setup_graph(owner)
    expiry_locked, review_entered, release_expiry = Event(), Event(), Event()
    failures = []

    def expire():
        db = SessionLocal()
        try:
            old = db.query(Resume).filter_by(id=old_id).with_for_update().one()
            old.version += 1
            old.deleted_at = datetime.utcnow()
            old.delist_reason = "expired"
            db.flush()
            expiry_locked.set()
            assert release_expiry.wait(10)
            db.commit()
        except BaseException as exc:  # surfaced in the parent thread
            failures.append(exc)
            db.rollback()
        finally:
            db.close()

    def review():
        assert expiry_locked.wait(10)
        db = SessionLocal()
        try:
            review_entered.set()
            relation, _, graph = lock_replacement_graph(db, relation_id)
            relation.review_outcome = "passed"
            assert resume_replace_service.activate_replacement_locked(
                db, relation, graph[old_id], graph[new_id], expected_old_version=2,
            )
            db.commit()
        except BaseException as exc:
            failures.append(exc)
            db.rollback()
        finally:
            db.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(expire)
            second = pool.submit(review)
            assert review_entered.wait(10)
            release_expiry.set()
            first.result(timeout=10)
            second.result(timeout=10)
        assert failures == []
        db = SessionLocal()
        try:
            old, new = db.get(Resume, old_id), db.get(Resume, new_id)
            assert old.version == 3 and old.delist_reason == "expired"
            assert new.activated_at is not None
            assert db.query(Resume).filter_by(owner_userid=owner, deleted_at=None, audit_status="passed").count() == 1
        finally:
            db.close()
    finally:
        release_expiry.set()
        _cleanup(owner)


def test_review_lock_first_then_expiry_observes_replaced_without_deadlock():
    owner = f"stage4-review-first-{uuid4().hex}"
    relation_id, old_id, new_id = _setup_graph(owner)
    review_locked, expiry_entered, release_review = Event(), Event(), Event()
    failures = []

    def review():
        db = SessionLocal()
        try:
            relation, _, graph = lock_replacement_graph(db, relation_id)
            review_locked.set()
            assert release_review.wait(10)
            relation.review_outcome = "passed"
            assert resume_replace_service.activate_replacement_locked(
                db, relation, graph[old_id], graph[new_id], expected_old_version=2,
            )
            db.commit()
        except BaseException as exc:
            failures.append(exc)
            db.rollback()
        finally:
            db.close()

    def expire():
        assert review_locked.wait(10)
        db = SessionLocal()
        try:
            expiry_entered.set()
            old = db.query(Resume).filter_by(id=old_id).with_for_update().one()
            if old.deleted_at is None and old.delist_reason is None:
                old.version += 1
                old.deleted_at = datetime.utcnow()
                old.delist_reason = "expired"
            db.commit()
        except BaseException as exc:
            failures.append(exc)
            db.rollback()
        finally:
            db.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(review)
            second = pool.submit(expire)
            assert expiry_entered.wait(10)
            release_review.set()
            first.result(timeout=10)
            second.result(timeout=10)
        assert failures == []
        db = SessionLocal()
        try:
            old, new = db.get(Resume, old_id), db.get(Resume, new_id)
            assert old.version == 3 and old.delist_reason == "replaced"
            assert new.activated_at is not None
            assert db.query(Resume).filter_by(owner_userid=owner, deleted_at=None, audit_status="passed").count() == 1
        finally:
            db.close()
    finally:
        release_review.set()
        _cleanup(owner)


def test_cleanup_failure_rolls_back_graph_on_mysql(monkeypatch):
    owner = f"stage4-rollback-{uuid4().hex}"
    relation_id, old_id, new_id = _setup_graph(owner)
    db = SessionLocal()
    try:
        relation, _, graph = lock_replacement_graph(db, relation_id)
        relation.review_outcome = "passed"
        monkeypatch.setattr(
            resume_replace_service, "ensure_target_cleanup_task",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
        )
        with pytest.raises(RuntimeError, match="cleanup failed"):
            resume_replace_service.activate_replacement_locked(
                db, relation, graph[old_id], graph[new_id], expected_old_version=2,
            )
        db.rollback()
    finally:
        db.close()

    try:
        verify = SessionLocal()
        try:
            relation = verify.get(ResumeReplacement, relation_id)
            old, new = verify.get(Resume, old_id), verify.get(Resume, new_id)
            assert relation.review_outcome == "pending" and relation.lifecycle_status == "awaiting_review"
            assert old.deleted_at is None and old.delist_reason is None and old.version == 2
            assert new.activated_at is None and new.expires_at is None
            assert verify.query(TargetCleanupTask).filter_by(target_type="resume", target_id=old_id).count() == 0
        finally:
            verify.close()
    finally:
        _cleanup(owner)
