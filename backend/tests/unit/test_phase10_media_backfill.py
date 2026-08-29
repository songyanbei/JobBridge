from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateTable

from app.config import settings
from app.models import Job, MediaAssetLifecycle, Resume, SystemConfig
from scripts import backfill_media_lifecycle, phase10_preflight


@compiles(mysql.TINYINT, "sqlite")
@compiles(mysql.SMALLINT, "sqlite")
@compiles(mysql.INTEGER, "sqlite")
@compiles(mysql.BIGINT, "sqlite")
def _compile_mysql_integer_for_sqlite(_type, _compiler, **_kwargs):
    return "INTEGER"


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        for table in (
            Job.__table__,
            Resume.__table__,
            MediaAssetLifecycle.__table__,
            SystemConfig.__table__,
        ):
            connection.execute(CreateTable(table))
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _job(db, job_id: int, *, images, deleted_at=None):
    row = Job(
        id=job_id,
        owner_userid="owner-1",
        city="苏州",
        job_category="普工",
        salary_floor_monthly=5000,
        pay_type="月薪",
        headcount=10,
        gender_required="不限",
        is_long_term=True,
        raw_text="job",
        images=images,
        audit_status="passed",
        activated_at=_now(),
        expires_at=_now() + timedelta(days=30),
        deleted_at=deleted_at,
        version=1,
    )
    db.add(row)
    db.commit()
    return row


def _resume(db, resume_id: int, *, images, deleted_at=None):
    row = Resume(
        id=resume_id,
        owner_userid="owner-1",
        expected_cities=["苏州"],
        expected_job_categories=["普工"],
        salary_expect_floor_monthly=5000,
        gender="男",
        age=25,
        accept_long_term=True,
        accept_short_term=False,
        raw_text="resume",
        images=images,
        audit_status="passed",
        expires_at=_now() + timedelta(days=30),
        deleted_at=deleted_at,
        version=1,
    )
    db.add(row)
    db.commit()
    return row


def test_backfill_is_dry_run_by_default_and_apply_is_idempotent(db, monkeypatch):
    monkeypatch.setattr(settings, "oss_trusted_origins", "https://assets.example.com")
    _job(db, 1, images=[
        "images/job/a.jpg",
        "/files/images/job/a.jpg",
        "https://assets.example.com/files/images/job/b.jpg?sig=secret",
    ])
    _resume(
        db,
        2,
        images=["images/resume/a.jpg"],
        deleted_at=_now() - timedelta(days=8),
    )

    dry = backfill_media_lifecycle.backfill_media_lifecycle(db)

    assert dry["apply"] is False
    assert dry["normalized_job_image_key_count"] == 2
    assert dry["normalized_resume_image_key_count"] == 1
    assert dry["media_reference_alias_count"] == 1
    assert dry["missing_media_lifecycle_key_count"] == 3
    assert db.query(MediaAssetLifecycle).count() == 0

    applied = backfill_media_lifecycle.backfill_media_lifecycle(
        db, apply=True, migration_batch_id="00000000-0000-0000-0000-000000000001"
    )

    assert applied["created_media_lifecycle_count"] == 3
    assert applied["missing_media_lifecycle_key_count"] == 0
    states = {
        row.object_key: row.state for row in db.query(MediaAssetLifecycle).all()
    }
    assert states == {
        "images/job/a.jpg": "attached",
        "images/job/b.jpg": "attached",
        "images/resume/a.jpg": "attached",
    }

    repeated = backfill_media_lifecycle.backfill_media_lifecycle(db, apply=True)
    assert repeated["created_media_lifecycle_count"] == 0
    assert repeated["updated_media_lifecycle_count"] == 0
    assert repeated["matched_media_lifecycle_key_count"] == 3
    assert db.query(MediaAssetLifecycle).count() == 3


def test_backfill_keeps_recent_soft_deleted_media_until_configured_delay(db):
    db.add(SystemConfig(
        config_key="ttl.hard_delete.delay_days",
        config_value="14",
        value_type="int",
    ))
    _job(
        db,
        1,
        images=["images/job/recent.jpg"],
        deleted_at=_now() - timedelta(days=8),
    )
    _job(
        db,
        2,
        images=["images/job/due.jpg"],
        deleted_at=_now() - timedelta(days=15),
    )

    report = backfill_media_lifecycle.backfill_media_lifecycle(db, apply=True)

    assert report["hard_delete_delay_days"] == 14
    assert report["non_deleted_soft_deleted_media_key_count"] == 1
    rows = {
        row.object_key: row for row in db.query(MediaAssetLifecycle).all()
    }
    assert rows["images/job/recent.jpg"].state == "attached"
    assert rows["images/job/recent.jpg"].next_attempt_at is None
    assert rows["images/job/due.jpg"].state == "attached"
    assert rows["images/job/due.jpg"].next_attempt_at is None


def test_backfill_restores_only_unstarted_recent_delete_pending_media(db):
    _job(db, 1, images=["images/job/restorable.jpg"], deleted_at=_now())
    _job(db, 2, images=["images/job/started.jpg"], deleted_at=_now())
    restorable = MediaAssetLifecycle(
        object_key="images/job/restorable.jpg",
        owner_userid="owner-1",
        entity_type="job",
        entity_id=1,
        state="delete_pending",
        next_attempt_at=_now(),
    )
    started = MediaAssetLifecycle(
        object_key="images/job/started.jpg",
        owner_userid="owner-1",
        entity_type="job",
        entity_id=2,
        state="delete_pending",
        next_attempt_at=_now(),
        attempt_count=1,
    )
    db.add_all([restorable, started])
    db.commit()

    report = backfill_media_lifecycle.backfill_media_lifecycle(db, apply=True)

    db.refresh(restorable)
    db.refresh(started)
    assert restorable.state == "attached"
    assert restorable.next_attempt_at is None
    assert started.state == "delete_pending"
    assert report["updated_media_lifecycle_count"] == 1
    assert report["media_reference_conflict_count"] == 1


def test_backfill_preserves_immediate_cleanup_for_unactivated_candidates(db):
    candidate = _job(
        db,
        1,
        images=["images/job/candidate.jpg"],
        deleted_at=_now(),
    )
    candidate.audit_status = "rejected"
    candidate.activated_at = None
    candidate.expires_at = None
    candidate.candidate_expires_at = _now() - timedelta(minutes=1)
    db.add(MediaAssetLifecycle(
        object_key="images/job/candidate.jpg",
        owner_userid="owner-1",
        entity_type="job",
        entity_id=1,
        state="attached",
    ))
    db.commit()

    report = backfill_media_lifecycle.backfill_media_lifecycle(db, apply=True)

    media = db.query(MediaAssetLifecycle).one()
    assert media.state == "delete_pending"
    assert media.next_attempt_at is not None
    assert report["updated_media_lifecycle_count"] == 1
    assert report["non_deleted_soft_deleted_media_key_count"] == 1


@pytest.mark.parametrize(
    ("expires_at", "candidate_expires_at"),
    [(_now(), _now()), (None, None)],
)
def test_backfill_does_not_immediately_delete_invalid_candidate_shapes(
    db, expires_at, candidate_expires_at,
):
    job = _job(db, 1, images=["images/job/invalid-shape.jpg"], deleted_at=_now())
    job.audit_status = "rejected"
    job.activated_at = None
    job.expires_at = expires_at
    job.candidate_expires_at = candidate_expires_at
    db.commit()

    report = backfill_media_lifecycle.backfill_media_lifecycle(db, apply=True)

    media = db.query(MediaAssetLifecycle).one()
    assert media.state == "attached"
    assert media.next_attempt_at is None
    assert report["non_deleted_soft_deleted_media_key_count"] == 0


def test_backfill_uses_database_time_and_strict_cutoff(db, monkeypatch):
    database_now = datetime(2026, 8, 14, 12, 0, 0)
    monkeypatch.setattr(
        backfill_media_lifecycle,
        "_database_now",
        lambda _db: database_now,
    )
    cutoff = database_now - timedelta(days=7)
    _job(db, 1, images=["images/job/before.jpg"], deleted_at=cutoff - timedelta(microseconds=1))
    _job(db, 2, images=["images/job/at.jpg"], deleted_at=cutoff)
    _job(db, 3, images=["images/job/after.jpg"], deleted_at=cutoff + timedelta(microseconds=1))

    report = backfill_media_lifecycle.backfill_media_lifecycle(db, apply=True)

    assert report["non_deleted_soft_deleted_media_key_count"] == 1
    assert {
        row.state for row in db.query(MediaAssetLifecycle).all()
    } == {"attached"}


def test_backfill_reports_unresolved_conflicts_and_invalid_images(
    db, monkeypatch, tmp_path,
):
    monkeypatch.setattr(settings, "oss_trusted_origins", "")
    _job(db, 1, images=["images/shared.jpg", "https://external.example/a.jpg"])
    _resume(db, 2, images=["images/shared.jpg"])
    _job(db, 3, images="{broken")

    report = backfill_media_lifecycle.backfill_media_lifecycle(db)

    assert report["media_reference_conflict_count"] == 1
    assert report["unresolved_media_reference_count"] == 1
    assert report["invalid_images_json_count"] == 1
    results = {item["result"] for item in report["details"]}
    assert {"conflict", "unresolved"}.issubset(results)

    applied = backfill_media_lifecycle.backfill_media_lifecycle(db, apply=True)
    assert applied["media_reference_conflict_count"] == 1
    assert applied["created_media_lifecycle_count"] == 0
    assert db.query(MediaAssetLifecycle).count() == 0

    paths = backfill_media_lifecycle.write_reports(report, tmp_path)
    assert Path(paths["detail_csv"]).exists()
    assert Path(paths["summary_json"]).exists()


def test_preflight_media_coverage_blocks_until_soft_deleted_media_is_deleted(db):
    _job(db, 1, images=["images/job/a.jpg"])
    _resume(
        db,
        2,
        images=["images/resume/a.jpg"],
        deleted_at=_now() - timedelta(days=8),
    )

    missing = phase10_preflight.collect_media_coverage(db)
    assert missing["missing_media_lifecycle_key_count"] == 2

    backfill_media_lifecycle.backfill_media_lifecycle(db, apply=True)
    pending = phase10_preflight.collect_media_coverage(db)
    assert pending["missing_media_lifecycle_key_count"] == 0
    assert pending["non_deleted_soft_deleted_media_key_count"] == 1
    assert all(pending[name] == 0 for name in phase10_preflight.MEDIA_BLOCKING_CHECKS)

    media = db.query(MediaAssetLifecycle).filter_by(object_key="images/resume/a.jpg").one()
    media.state = "deleted"
    media.deleted_at = _now()
    db.commit()
    ready = phase10_preflight.collect_media_coverage(db)
    assert all(ready[name] == 0 for name in phase10_preflight.MEDIA_BLOCKING_CHECKS)


def test_backfill_reconciles_unreferenced_media_bound_to_soft_deleted_entity(db):
    _job(db, 1, images=[], deleted_at=_now() - timedelta(days=8))
    media = MediaAssetLifecycle(
        object_key="images/job/orphaned-reference.jpg",
        operation_id="op-1",
        owner_userid="owner-1",
        entity_type="job",
        entity_id=1,
        state="attached",
    )
    db.add(media)
    db.commit()

    dry = backfill_media_lifecycle.backfill_media_lifecycle(db)
    assert dry["repair_required_media_lifecycle_key_count"] == 0
    assert dry["non_deleted_soft_deleted_media_key_count"] == 1
    assert media.state == "attached"

    applied = backfill_media_lifecycle.backfill_media_lifecycle(db, apply=True)
    assert applied["updated_media_lifecycle_count"] == 0
    assert applied["non_deleted_soft_deleted_media_key_count"] == 1
    assert media.state == "attached"
    assert media.next_attempt_at is None

    pending = phase10_preflight.collect_media_coverage(db)
    assert pending["non_deleted_soft_deleted_media_key_count"] == 1
    assert pending["repair_required_media_lifecycle_key_count"] == 0
    assert all(pending[name] == 0 for name in phase10_preflight.MEDIA_BLOCKING_CHECKS)

    media.state = "deleted"
    media.deleted_at = _now()
    db.commit()
    ready = phase10_preflight.collect_media_coverage(db)
    assert all(ready[name] == 0 for name in phase10_preflight.MEDIA_BLOCKING_CHECKS)


def test_backfill_never_revives_dead_letter_for_soft_deleted_entity(db):
    _resume(
        db,
        3,
        images=["images/resume/dead.jpg"],
        deleted_at=_now() - timedelta(days=8),
    )
    media = MediaAssetLifecycle(
        object_key="images/resume/dead.jpg",
        owner_userid="owner-1",
        entity_type="resume",
        entity_id=3,
        state="dead_letter",
        attempt_count=10,
        last_error="operator action required",
    )
    db.add(media)
    db.commit()

    report = backfill_media_lifecycle.backfill_media_lifecycle(db, apply=True)

    db.refresh(media)
    assert media.state == "dead_letter"
    assert media.attempt_count == 10
    assert media.next_attempt_at is None
    assert report["updated_media_lifecycle_count"] == 0
    assert report["repair_required_media_lifecycle_key_count"] == 0
    assert report["non_deleted_soft_deleted_media_key_count"] == 1
    assert report["media_delete_dead_letter_key_count"] == 1
    detail = next(
        item for item in report["details"]
        if item["normalized_object_key"] == media.object_key
    )
    assert detail["error_code"] == "media_delete_dead_letter_requires_manual_recovery"


def test_backfill_reports_active_entity_dead_letter_as_conflict(db):
    _job(db, 4, images=["images/job/dead.jpg"])
    media = MediaAssetLifecycle(
        object_key="images/job/dead.jpg",
        owner_userid="owner-1",
        entity_type="job",
        entity_id=4,
        state="dead_letter",
        attempt_count=10,
    )
    db.add(media)
    db.commit()

    report = backfill_media_lifecycle.backfill_media_lifecycle(db, apply=True)

    db.refresh(media)
    assert media.state == "dead_letter"
    assert report["media_reference_conflict_count"] == 1
    assert report["media_delete_dead_letter_key_count"] == 1
    detail = next(
        item for item in report["details"]
        if item["normalized_object_key"] == media.object_key
    )
    assert detail["error_code"] == "active_entity_media_state_dead_letter"


def test_dead_letter_gate_counts_all_lifecycle_rows_globally(db):
    _job(db, 1, images=["images/job/referenced-dead.jpg"])
    db.add_all([
        MediaAssetLifecycle(
            object_key="images/job/referenced-dead.jpg",
            owner_userid="owner-1",
            entity_type="job",
            entity_id=1,
            state="dead_letter",
        ),
        MediaAssetLifecycle(
            object_key="images/job/active-unreferenced-dead.jpg",
            owner_userid="owner-1",
            entity_type="job",
            entity_id=1,
            state="dead_letter",
        ),
        MediaAssetLifecycle(
            object_key="images/job/missing-entity-dead.jpg",
            owner_userid="owner-1",
            entity_type="job",
            entity_id=999,
            state="dead_letter",
        ),
        MediaAssetLifecycle(
            object_key="images/draft/unbound-dead.jpg",
            owner_userid="owner-1",
            state="dead_letter",
        ),
    ])
    db.commit()

    report = backfill_media_lifecycle.backfill_media_lifecycle(db)
    coverage = phase10_preflight.collect_media_coverage(db)

    assert report["media_delete_dead_letter_key_count"] == 4
    assert coverage["media_delete_dead_letter_key_count"] == 4
    assert "media_delete_dead_letter_key_count" in phase10_preflight.MEDIA_BLOCKING_CHECKS


def test_backfill_does_not_delete_unreferenced_key_used_by_another_entity(db):
    _job(db, 1, images=[], deleted_at=_now())
    _resume(db, 2, images=["images/shared-live.jpg"])
    media = MediaAssetLifecycle(
        object_key="images/shared-live.jpg",
        operation_id="op-2",
        owner_userid="owner-1",
        entity_type="job",
        entity_id=1,
        state="attached",
    )
    db.add(media)
    db.commit()

    report = backfill_media_lifecycle.backfill_media_lifecycle(db, apply=True)

    assert report["media_reference_conflict_count"] == 1
    assert media.state == "attached"
