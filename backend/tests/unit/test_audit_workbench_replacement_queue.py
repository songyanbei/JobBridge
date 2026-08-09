from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import MetaData, create_engine
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.api.admin.audit import _serialize_queue_item
from app.core.exceptions import BusinessException
from app.models import Job, JobReplacement, Resume, User
from app.services import audit_workbench_service as workbench
from app.services.job_admin_service import replacement_projections


@compiles(mysql.TINYINT, "sqlite")
@compiles(mysql.SMALLINT, "sqlite")
@compiles(mysql.INTEGER, "sqlite")
@compiles(mysql.BIGINT, "sqlite")
def _compile_mysql_integer_for_sqlite(_type, _compiler, **_kwargs):
    return "INTEGER"


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    for table in (User.__table__, Job.__table__):
        table.create(engine)
    # MySQL permits Job and Resume to each own an `idx_owner`; SQLite index
    # names are database-wide, so use an index-free metadata clone for Resume.
    resume_metadata = MetaData()
    User.__table__.to_metadata(resume_metadata)
    resume_table = Resume.__table__.to_metadata(resume_metadata)
    resume_table.indexes.clear()
    resume_table.create(engine)
    JobReplacement.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    session.add(User(external_userid="owner-1", role="factory"))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _job(db, *, audit_status="pending", active=False, raw_text="岗位"):
    row = Job(
        owner_userid="owner-1",
        city="苏州",
        job_category="普工",
        salary_floor_monthly=5000,
        pay_type="月薪",
        headcount=10,
        gender_required="不限",
        is_long_term=True,
        raw_text=raw_text,
        description=raw_text,
        audit_status=audit_status,
        activated_at=datetime.now() - timedelta(days=1) if active else None,
        expires_at=datetime.now() + timedelta(days=30) if active else None,
        candidate_expires_at=None if active else datetime.now() + timedelta(days=7),
        version=1,
    )
    db.add(row)
    db.flush()
    return row


def _replacement(
    db,
    *,
    old_job_id,
    new_job_id,
    suffix,
    review_outcome,
    lifecycle_status,
):
    relation = JobReplacement(
        operation_id=f"op-{suffix}",
        source_msg_id=f"msg-{suffix}",
        owner_userid="owner-1",
        old_job_id=old_job_id,
        new_job_id=new_job_id,
        old_job_version=1,
        old_business_digest=f"digest-{suffix}",
        review_outcome=review_outcome,
        lifecycle_status=lifecycle_status,
        active_old_job_id=(
            old_job_id if lifecycle_status in {"awaiting_review", "conflict"} else None
        ),
    )
    db.add(relation)
    db.flush()
    return relation


def _seed_queue_states(db):
    ordinary = _job(db, raw_text="普通待审岗位")

    awaiting_old = _job(db, audit_status="passed", active=True, raw_text="待审旧岗位")
    awaiting = _job(db, raw_text="replacement 待审候选")
    awaiting_relation = _replacement(
        db,
        old_job_id=awaiting_old.id,
        new_job_id=awaiting.id,
        suffix="awaiting",
        review_outcome="pending",
        lifecycle_status="awaiting_review",
    )

    conflict_old = _job(db, audit_status="passed", active=True, raw_text="冲突旧岗位")
    conflict = _job(db, raw_text="replacement 冲突候选")
    conflict_relation = _replacement(
        db,
        old_job_id=conflict_old.id,
        new_job_id=conflict.id,
        suffix="conflict",
        review_outcome="passed",
        lifecycle_status="conflict",
    )

    closed_old = _job(db, audit_status="passed", active=True, raw_text="已关闭旧岗位")
    closed = _job(db, raw_text="异常保留 pending 的已关闭候选")
    _replacement(
        db,
        old_job_id=closed_old.id,
        new_job_id=closed.id,
        suffix="closed",
        review_outcome="passed",
        lifecycle_status="closed",
    )
    db.commit()
    return ordinary, awaiting, awaiting_relation, conflict, conflict_relation, closed


def test_pending_queue_and_count_exclude_reviewed_replacement_candidates(db, monkeypatch):
    ordinary, awaiting, relation, conflict, _, closed = _seed_queue_states(db)
    monkeypatch.setattr(workbench, "_risk_level", lambda *_args: ("low", []))
    monkeypatch.setattr(workbench, "get_audit_lock_holder", lambda *_args: None)

    items, total = workbench.list_queue(
        db, status="pending", target_type="job", page=1, size=20,
    )
    pending_count = workbench.get_pending_count(db)

    assert total == 2
    assert {item.obj.id for item in items} == {ordinary.id, awaiting.id}
    assert conflict.id not in {item.obj.id for item in items}
    assert closed.id not in {item.obj.id for item in items}
    assert pending_count == {"job": 2, "resume": 0, "total": 2}

    conflict_projection = replacement_projections(db, [conflict])[conflict.id]
    assert conflict_projection["replacement_review_outcome"] == "passed"
    assert conflict_projection["replacement_lifecycle_status"] == "conflict"

    replacement_item = next(item for item in items if item.obj.id == awaiting.id)
    assert replacement_item.replacement_id == relation.id
    assert replacement_item.replacement_review_outcome == "pending"
    assert replacement_item.replacement_lifecycle_status == "awaiting_review"
    payload = _serialize_queue_item(replacement_item)
    assert payload["replacement_id"] == relation.id
    assert payload["replacement_review_outcome"] == "pending"
    assert payload["replacement_lifecycle_status"] == "awaiting_review"


@pytest.mark.parametrize("action", ["pass", "reject"])
def test_conflict_candidate_cannot_be_reviewed_again_through_service(db, action):
    _, _, _, candidate, relation, _ = _seed_queue_states(db)

    with pytest.raises(BusinessException, match="replacement_already_reviewed"):
        if action == "pass":
            workbench.pass_action(db, "job", candidate.id, candidate.version, "reviewer")
        else:
            workbench.reject_action(
                db,
                "job",
                candidate.id,
                candidate.version,
                "不得重复审核",
                "reviewer",
            )
    db.rollback()
    db.refresh(candidate)
    db.refresh(relation)

    assert candidate.audit_status == "pending"
    assert relation.review_outcome == "passed"
    assert relation.lifecycle_status == "conflict"


def test_audit_frontend_guards_reviewed_replacements_before_actions():
    source = (
        Path(__file__).resolve().parents[3]
        / "frontend/src/views/audit/AuditWorkbenchView.vue"
    ).read_text(encoding="utf-8")

    assert "selectedRows.value.every(isQueueItemAuditable)" in source
    assert "isDetailAuditable(row.target_type, d)" in source
    assert ':disabled="!canEdit"' in source
    assert "&& isDetailAuditable(currentItem.value.target_type, detail.value)" in source
