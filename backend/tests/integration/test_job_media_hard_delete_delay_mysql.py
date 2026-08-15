"""Real MySQL coverage for deferred Job media deletion."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading
import time
from uuid import uuid4

import pytest
from sqlalchemy import event, text

from app.config import settings
from app.db import SessionLocal
from app.models import Job, MediaAssetLifecycle, TargetCleanupTask, User
from app.services.job_media_service import mark_job_media_delete_pending
from app.tasks import job_expiry_cleanup, media_cleanup_worker, ttl_cleanup
from scripts import backfill_media_lifecycle
from scripts.phase10_preflight import CHECKS


pytestmark = pytest.mark.integration


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def test_job_media_stays_attached_until_hard_delete_delay(monkeypatch):
    db = SessionLocal()
    owner = f"media-delay-{uuid4().hex}"
    job_id = None
    try:
        now = _now()
        db.add(User(external_userid=owner, role="factory"))
        db.commit()
        job = Job(
            owner_userid=owner,
            city="media-delay",
            job_category="media-delay",
            salary_floor_monthly=5000,
            pay_type=Job.__table__.c.pay_type.type.enums[0],
            headcount=1,
            raw_text="deferred media deletion",
            images=["images/job/deferred.jpg"],
            audit_status="passed",
            activated_at=now - timedelta(days=30),
            expires_at=now - timedelta(minutes=1),
            version=1,
        )
        db.add(job)
        db.flush()
        job_id = int(job.id)
        media = MediaAssetLifecycle(
            object_key="images/job/deferred.jpg",
            owner_userid=owner,
            entity_type="job",
            entity_id=job_id,
            state="attached",
        )
        db.add(media)
        db.commit()

        assert job_id in job_expiry_cleanup.expire_locked_batch(db, now=now)
        db.refresh(media)
        assert media.state == "attached"
        assert media.next_attempt_at is None
        claimed = media_cleanup_worker._claim_ids(db, "worker-before-delay", now, 10)
        assert int(media.id) not in claimed

        media.state = "delete_pending"
        media.next_attempt_at = now
        db.commit()
        backfill_media_lifecycle.backfill_media_lifecycle(db, apply=True)
        db.refresh(media)
        assert media.state == "attached"
        assert media.next_attempt_at is None

        monkeypatch.setattr(settings, "job_hard_delete_enabled", False)
        assert ttl_cleanup._hard_delete_expired_jobs(db, 0) == 0
        db.refresh(media)
        assert media.state == "attached"

        job = db.get(Job, job_id)
        job.deleted_at = now - timedelta(days=8)
        db.commit()
        backfill_media_lifecycle.backfill_media_lifecycle(db, apply=True)
        db.refresh(media)
        assert media.state == "attached"
        assert media.next_attempt_at is None
        claimed = media_cleanup_worker._claim_ids(db, "worker-after-delay", now, 10)
        assert int(media.id) not in claimed
        monkeypatch.setattr(settings, "job_hard_delete_enabled", True)

        assert ttl_cleanup._hard_delete_expired_jobs(db, 7) == 0
        db.refresh(media)
        assert media.state == "delete_pending"
        assert media.next_attempt_at is not None
        assert db.get(Job, job_id) is not None
    finally:
        db.rollback()
        if job_id is not None:
            db.query(TargetCleanupTask).filter(
                TargetCleanupTask.target_type == "job",
                TargetCleanupTask.target_id == job_id,
            ).delete(synchronize_session=False)
            db.query(MediaAssetLifecycle).filter(
                MediaAssetLifecycle.entity_type == "job",
                MediaAssetLifecycle.entity_id == job_id,
            ).delete(synchronize_session=False)
            db.query(Job).filter(Job.id == job_id).delete(synchronize_session=False)
        db.query(User).filter(User.external_userid == owner).delete(
            synchronize_session=False,
        )
        db.commit()
        db.close()


def test_hard_delete_delay_preflight_rejects_missing_and_invalid_values():
    db = SessionLocal()
    try:
        sql = text(CHECKS["invalid_hard_delete_delay_config"])
        for value, expected in (
            ("0", 0),
            ("3650", 0),
            ("-1", 1),
            ("3651", 1),
            ("invalid", 1),
        ):
            db.execute(text(
                "INSERT INTO system_config "
                "(config_key, config_value, value_type, description) "
                "VALUES ('ttl.hard_delete.delay_days', :value, 'int', 'integration test') "
                "ON DUPLICATE KEY UPDATE config_value=VALUES(config_value)"
            ), {"value": value})
            assert int(db.execute(sql).scalar_one()) == expected, value

        db.execute(text(
            "DELETE FROM system_config "
            "WHERE config_key='ttl.hard_delete.delay_days'"
        ))
        assert int(db.execute(sql).scalar_one()) == 1
    finally:
        db.rollback()
        db.close()


def _pause_after_job_scan(engine, worker_thread, scanned, resume):
    seen = {"count": 0}

    def listener(_conn, _cursor, statement, _params, _context, _many):
        if threading.current_thread() is not worker_thread:
            return
        normalized = " ".join(statement.lower().split())
        if " from job " not in normalized or "job.id >" not in normalized:
            return
        if "for update" in normalized:
            return
        seen["count"] += 1
        if seen["count"] == 2:
            scanned.set()
            assert resume.wait(timeout=10)

    event.listen(engine, "after_cursor_execute", listener)
    return listener


def test_backfill_rechecks_candidate_after_concurrent_cleanup():
    setup = SessionLocal()
    engine = setup.get_bind()
    owner = f"media-candidate-race-{uuid4().hex}"
    scanned = threading.Event()
    resume = threading.Event()
    errors = []
    job_id = None
    worker_thread = None
    listener = None
    try:
        now = _now()
        setup.add(User(external_userid=owner, role="factory"))
        setup.commit()
        candidate = Job(
            owner_userid=owner,
            city="candidate-race",
            job_category="candidate-race",
            salary_floor_monthly=5000,
            pay_type=Job.__table__.c.pay_type.type.enums[0],
            headcount=1,
            raw_text="candidate cleanup race",
            images=["images/job/candidate-race.jpg"],
            audit_status="pending",
            candidate_expires_at=now + timedelta(days=1),
            version=1,
        )
        setup.add(candidate)
        setup.flush()
        job_id = int(candidate.id)
        setup.add(MediaAssetLifecycle(
            object_key="images/job/candidate-race.jpg",
            owner_userid=owner,
            entity_type="job",
            entity_id=job_id,
            state="attached",
        ))
        setup.commit()

        def run_backfill():
            db = SessionLocal()
            try:
                backfill_media_lifecycle.backfill_media_lifecycle(db, apply=True)
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                db.rollback()
                db.close()

        worker_thread = threading.Thread(target=run_backfill)
        listener = _pause_after_job_scan(
            engine,
            worker_thread,
            scanned,
            resume,
        )
        worker_thread.start()
        assert scanned.wait(timeout=10)

        concurrent = SessionLocal()
        try:
            current = concurrent.query(Job).filter(Job.id == job_id).with_for_update().one()
            current.deleted_at = _now()
            mark_job_media_delete_pending(concurrent, job_id, include_pending=True)
            concurrent.commit()
        finally:
            concurrent.close()
        resume.set()
        worker_thread.join(timeout=10)
        assert not worker_thread.is_alive()
        assert errors == []

        setup.expire_all()
        media = setup.query(MediaAssetLifecycle).filter_by(entity_id=job_id).one()
        assert media.state == "delete_pending"
        assert media.next_attempt_at is not None
    finally:
        resume.set()
        if worker_thread is not None:
            worker_thread.join(timeout=10)
        if listener is not None:
            event.remove(engine, "after_cursor_execute", listener)
        setup.rollback()
        if job_id is not None:
            setup.query(MediaAssetLifecycle).filter_by(entity_id=job_id).delete()
            setup.query(Job).filter_by(id=job_id).delete()
        setup.query(User).filter_by(external_userid=owner).delete()
        setup.commit()
        setup.close()


def test_backfill_rechecks_database_time_after_cutoff_crosses():
    setup = SessionLocal()
    engine = setup.get_bind()
    owner = f"media-cutoff-race-{uuid4().hex}"
    scanned = threading.Event()
    resume = threading.Event()
    errors = []
    job_id = None
    worker_thread = None
    listener = None
    try:
        database_now = setup.execute(text("SELECT NOW(6)")).scalar_one()
        deleted_at = database_now - timedelta(days=7) + timedelta(seconds=1)
        setup.add(User(external_userid=owner, role="factory"))
        setup.commit()
        job = Job(
            owner_userid=owner,
            city="cutoff-race",
            job_category="cutoff-race",
            salary_floor_monthly=5000,
            pay_type=Job.__table__.c.pay_type.type.enums[0],
            headcount=1,
            raw_text="cutoff crossing race",
            images=["images/job/cutoff-race.jpg"],
            audit_status="passed",
            activated_at=database_now - timedelta(days=30),
            expires_at=database_now - timedelta(days=8),
            deleted_at=deleted_at,
            delist_reason="expired",
            version=2,
        )
        setup.add(job)
        setup.flush()
        job_id = int(job.id)
        setup.add(MediaAssetLifecycle(
            object_key="images/job/cutoff-race.jpg",
            owner_userid=owner,
            entity_type="job",
            entity_id=job_id,
            state="delete_pending",
            next_attempt_at=database_now,
        ))
        setup.commit()

        def run_backfill():
            db = SessionLocal()
            try:
                backfill_media_lifecycle.backfill_media_lifecycle(db, apply=True)
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                db.rollback()
                db.close()

        worker_thread = threading.Thread(target=run_backfill)
        listener = _pause_after_job_scan(
            engine,
            worker_thread,
            scanned,
            resume,
        )
        worker_thread.start()
        assert scanned.wait(timeout=10)
        time.sleep(2.2)
        assert deleted_at < setup.execute(text("SELECT NOW(6) - INTERVAL 7 DAY")).scalar_one()
        resume.set()
        worker_thread.join(timeout=10)
        assert not worker_thread.is_alive()
        assert errors == []

        setup.expire_all()
        media = setup.query(MediaAssetLifecycle).filter_by(entity_id=job_id).one()
        assert media.state == "delete_pending"
    finally:
        resume.set()
        if worker_thread is not None:
            worker_thread.join(timeout=10)
        if listener is not None:
            event.remove(engine, "after_cursor_execute", listener)
        setup.rollback()
        if job_id is not None:
            setup.query(MediaAssetLifecycle).filter_by(entity_id=job_id).delete()
            setup.query(Job).filter_by(id=job_id).delete()
        setup.query(User).filter_by(external_userid=owner).delete()
        setup.commit()
        setup.close()


def test_backfill_locks_reverse_image_media_in_global_id_order():
    setup = SessionLocal()
    engine = setup.get_bind()
    owner = f"media-lock-order-{uuid4().hex}"
    media_locked = threading.Event()
    release_backfill = threading.Event()
    errors = []
    job_id = None
    backfill_thread = None
    marker_thread = None
    observed_sql = []
    listener_added = False

    def listener(_conn, _cursor, statement, _params, _context, _many):
        if threading.current_thread() is not backfill_thread:
            return
        normalized = " ".join(statement.lower().split())
        if (
            "from media_asset_lifecycle" in normalized
            and "object_key in" in normalized
            and "for update" in normalized
        ):
            observed_sql.append(normalized)
            media_locked.set()
            assert release_backfill.wait(timeout=10)

    try:
        now = _now()
        setup.add(User(external_userid=owner, role="factory"))
        setup.commit()
        job = Job(
            owner_userid=owner,
            city="media-lock-order",
            job_category="media-lock-order",
            salary_floor_monthly=5000,
            pay_type=Job.__table__.c.pay_type.type.enums[0],
            headcount=1,
            raw_text="reverse image media lock order",
            images=["images/job/id-2.jpg", "images/job/id-1.jpg"],
            audit_status="passed",
            activated_at=now - timedelta(days=30),
            expires_at=now - timedelta(days=1),
            deleted_at=now,
            delist_reason="expired",
            version=2,
        )
        setup.add(job)
        setup.flush()
        job_id = int(job.id)
        setup.add_all([
            MediaAssetLifecycle(
                object_key="images/job/id-1.jpg",
                owner_userid=owner,
                entity_type="job",
                entity_id=job_id,
                state="attached",
            ),
            MediaAssetLifecycle(
                object_key="images/job/id-2.jpg",
                owner_userid=owner,
                entity_type="job",
                entity_id=job_id,
                state="attached",
            ),
        ])
        setup.commit()

        def run_backfill():
            db = SessionLocal()
            try:
                backfill_media_lifecycle.backfill_media_lifecycle(db, apply=True)
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                db.rollback()
                db.close()

        def run_marker():
            db = SessionLocal()
            try:
                mark_job_media_delete_pending(db, job_id)
                db.commit()
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                db.rollback()
                db.close()

        backfill_thread = threading.Thread(target=run_backfill)
        event.listen(engine, "after_cursor_execute", listener)
        listener_added = True
        backfill_thread.start()
        assert media_locked.wait(timeout=10)
        marker_thread = threading.Thread(target=run_marker)
        marker_thread.start()
        time.sleep(0.2)
        assert marker_thread.is_alive()
        release_backfill.set()
        backfill_thread.join(timeout=10)
        marker_thread.join(timeout=10)

        assert errors == []
        assert not backfill_thread.is_alive()
        assert not marker_thread.is_alive()
        assert observed_sql
        assert "order by media_asset_lifecycle.id" in observed_sql[0]
        setup.expire_all()
        states = {
            row.state
            for row in setup.query(MediaAssetLifecycle).filter_by(entity_id=job_id)
        }
        assert states == {"delete_pending"}
    finally:
        release_backfill.set()
        if backfill_thread is not None:
            backfill_thread.join(timeout=10)
        if marker_thread is not None:
            marker_thread.join(timeout=10)
        if listener_added:
            event.remove(engine, "after_cursor_execute", listener)
        setup.rollback()
        if job_id is not None:
            setup.query(MediaAssetLifecycle).filter_by(entity_id=job_id).delete()
            setup.query(Job).filter_by(id=job_id).delete()
        setup.query(User).filter_by(external_userid=owner).delete()
        setup.commit()
        setup.close()
