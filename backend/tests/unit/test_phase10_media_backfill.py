from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateTable

from app.config import settings
from app.models import Job, MediaAssetLifecycle, Resume
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
        for table in (Job.__table__, Resume.__table__, MediaAssetLifecycle.__table__):
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
    _resume(db, 2, images=["images/resume/a.jpg"], deleted_at=_now())

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
        "images/resume/a.jpg": "delete_pending",
    }

    repeated = backfill_media_lifecycle.backfill_media_lifecycle(db, apply=True)
    assert repeated["created_media_lifecycle_count"] == 0
    assert repeated["updated_media_lifecycle_count"] == 0
    assert repeated["matched_media_lifecycle_key_count"] == 3
    assert db.query(MediaAssetLifecycle).count() == 3


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
    _resume(db, 2, images=["images/resume/a.jpg"], deleted_at=_now())

    missing = phase10_preflight.collect_media_coverage(db)
    assert missing["missing_media_lifecycle_key_count"] == 2

    backfill_media_lifecycle.backfill_media_lifecycle(db, apply=True)
    pending = phase10_preflight.collect_media_coverage(db)
    assert pending["missing_media_lifecycle_key_count"] == 0
    assert pending["non_deleted_soft_deleted_media_key_count"] == 1

    media = db.query(MediaAssetLifecycle).filter_by(object_key="images/resume/a.jpg").one()
    media.state = "deleted"
    media.deleted_at = _now()
    db.commit()
    ready = phase10_preflight.collect_media_coverage(db)
    assert all(ready[name] == 0 for name in phase10_preflight.MEDIA_BLOCKING_CHECKS)


def test_backfill_reconciles_unreferenced_media_bound_to_soft_deleted_entity(db):
    _job(db, 1, images=[], deleted_at=_now())
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
    assert dry["repair_required_media_lifecycle_key_count"] == 1
    assert dry["non_deleted_soft_deleted_media_key_count"] == 1
    assert media.state == "attached"

    applied = backfill_media_lifecycle.backfill_media_lifecycle(db, apply=True)
    assert applied["updated_media_lifecycle_count"] == 1
    assert applied["non_deleted_soft_deleted_media_key_count"] == 1
    assert media.state == "delete_pending"
    assert media.next_attempt_at is not None

    pending = phase10_preflight.collect_media_coverage(db)
    assert pending["non_deleted_soft_deleted_media_key_count"] == 1
    assert pending["repair_required_media_lifecycle_key_count"] == 0

    media.state = "deleted"
    media.deleted_at = _now()
    db.commit()
    ready = phase10_preflight.collect_media_coverage(db)
    assert all(ready[name] == 0 for name in phase10_preflight.MEDIA_BLOCKING_CHECKS)


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
