"""Target cleanup backfill ordering and idempotency on real MySQL."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.db import SessionLocal
from app.models import Job, TargetCleanupTask, User
from scripts import backfill_target_cleanup_tasks


pytestmark = pytest.mark.integration


def test_target_cleanup_backfill_dry_run_apply_and_recheck():
    baseline = backfill_target_cleanup_tasks.run(apply=False, batch_size=1)
    assert baseline["missing"] == 0

    suffix = uuid4().hex[:12]
    userid = f"target-backfill-{suffix}"
    job_id = 0
    setup_db = SessionLocal()
    try:
        setup_db.add(User(external_userid=userid, role="factory"))
        setup_db.flush()
        job = Job(
            owner_userid=userid,
            city="N17",
            job_category="N17",
            salary_floor_monthly=5000,
            pay_type=Job.__table__.c.pay_type.type.enums[0],
            headcount=1,
            raw_text="target cleanup backfill integration",
            deleted_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        setup_db.add(job)
        setup_db.commit()
        job_id = int(job.id)

        dry_run = backfill_target_cleanup_tasks.run(apply=False, batch_size=1)
        assert dry_run["created"] == 0
        assert dry_run["missing"] == 1

        applied = backfill_target_cleanup_tasks.run(apply=True, batch_size=1)
        assert applied["created"] == 1
        assert applied["missing"] == 0

        repeated = backfill_target_cleanup_tasks.run(apply=True, batch_size=1)
        assert repeated["created"] == 0
        assert repeated["missing"] == 0

        setup_db.expire_all()
        task = setup_db.query(TargetCleanupTask).filter_by(
            target_type="job", target_id=job_id,
        ).one()
        assert task.reason == "historical_soft_delete"
    finally:
        setup_db.rollback()
        if job_id:
            setup_db.query(TargetCleanupTask).filter_by(
                target_type="job", target_id=job_id,
            ).delete(synchronize_session=False)
            setup_db.query(Job).filter(Job.id == job_id).delete(
                synchronize_session=False,
            )
        setup_db.query(User).filter(User.external_userid == userid).delete(
            synchronize_session=False,
        )
        setup_db.commit()
        setup_db.close()


def test_final_coverage_sees_low_id_soft_delete_from_second_rr_connection(
    monkeypatch,
):
    baseline = backfill_target_cleanup_tasks.run(apply=False, batch_size=1)
    assert baseline["missing"] == 0

    suffix = uuid4().hex[:12]
    userid = f"target-race-{suffix}"
    job_ids = []
    setup_db = SessionLocal()
    try:
        setup_db.add(User(external_userid=userid, role="factory"))
        setup_db.flush()
        low = Job(
            owner_userid=userid,
            city="N17",
            job_category="N17",
            salary_floor_monthly=5000,
            pay_type=Job.__table__.c.pay_type.type.enums[0],
            headcount=1,
            raw_text="target cleanup low id race",
        )
        high = Job(
            owner_userid=userid,
            city="N17",
            job_category="N17",
            salary_floor_monthly=5000,
            pay_type=Job.__table__.c.pay_type.type.enums[0],
            headcount=1,
            raw_text="target cleanup high id race",
            deleted_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        setup_db.add_all([low, high])
        setup_db.commit()
        job_ids = [int(low.id), int(high.id)]
        assert job_ids[0] < job_ids[1]

        real_upsert = backfill_target_cleanup_tasks.upsert_job_cleanup_task
        raced = False

        def _soft_delete_low_from_second_connection(db, job_id, **kwargs):
            nonlocal raced
            if int(job_id) == job_ids[1] and not raced:
                with SessionLocal() as racer_db:
                    changed = racer_db.query(Job).filter(
                        Job.id == job_ids[0],
                        Job.deleted_at.is_(None),
                    ).update({
                        "deleted_at": datetime.now(timezone.utc).replace(tzinfo=None),
                    }, synchronize_session=False)
                    assert changed == 1
                    racer_db.commit()
                raced = True
            return real_upsert(db, job_id, **kwargs)

        monkeypatch.setattr(
            backfill_target_cleanup_tasks,
            "upsert_job_cleanup_task",
            _soft_delete_low_from_second_connection,
        )

        raced_result = backfill_target_cleanup_tasks.run(
            apply=True, batch_size=1,
        )
        assert raced is True
        assert raced_result["created"] == 1
        assert raced_result["missing"] == 1

        monkeypatch.setattr(
            backfill_target_cleanup_tasks,
            "upsert_job_cleanup_task",
            real_upsert,
        )
        repaired = backfill_target_cleanup_tasks.run(apply=True, batch_size=1)
        assert repaired["created"] == 1
        assert repaired["missing"] == 0
    finally:
        setup_db.rollback()
        if job_ids:
            setup_db.query(TargetCleanupTask).filter(
                TargetCleanupTask.target_type == "job",
                TargetCleanupTask.target_id.in_(job_ids),
            ).delete(synchronize_session=False)
            setup_db.query(Job).filter(Job.id.in_(job_ids)).delete(
                synchronize_session=False,
            )
        setup_db.query(User).filter(User.external_userid == userid).delete(
            synchronize_session=False,
        )
        setup_db.commit()
        setup_db.close()
