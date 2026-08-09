import csv
from datetime import datetime, timedelta
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import MetaData, create_engine
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.api.admin.audit import _serialize_queue_item
from app.api.admin import jobs as jobs_api
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


def test_replacement_chain_projects_both_sides_in_list_detail_and_csv(db):
    job_a = _job(db, audit_status="passed", active=True, raw_text="chain-a")
    job_x = _job(db, raw_text="closed-chain-x")
    job_b = _job(db, audit_status="passed", active=True, raw_text="chain-b")
    job_c = _job(db, raw_text="chain-c")
    relation_ax = _replacement(
        db,
        old_job_id=job_a.id,
        new_job_id=job_x.id,
        suffix="chain-ax-closed",
        review_outcome="rejected",
        lifecycle_status="closed",
    )
    relation_ab = _replacement(
        db,
        old_job_id=job_a.id,
        new_job_id=job_b.id,
        suffix="chain-ab",
        review_outcome="passed",
        lifecycle_status="activated",
    )
    relation_bc = _replacement(
        db,
        old_job_id=job_b.id,
        new_job_id=job_c.id,
        suffix="chain-bc",
        review_outcome="pending",
        lifecycle_status="awaiting_review",
    )
    relation_ax.created_at = datetime(2026, 1, 1)
    relation_ax.updated_at = datetime(2026, 1, 4)
    relation_ab.created_at = datetime(2026, 1, 2)
    relation_ab.updated_at = datetime(2026, 1, 3)
    relation_bc.created_at = datetime(2026, 1, 3)
    relation_bc.updated_at = datetime(2026, 1, 3)
    db.commit()

    projections = replacement_projections(db, [job_a, job_b, job_c])
    assert projections[job_a.id] == {
        "replacement_id": relation_ab.id,
        "replacement_review_outcome": "passed",
        "replacement_lifecycle_status": "activated",
        "replacement_closed_reason": None,
        "replaces_job_id": None,
        "replaced_by_job_id": job_b.id,
    }
    assert projections[job_b.id] == {
        "replacement_id": relation_ab.id,
        "replacement_review_outcome": "passed",
        "replacement_lifecycle_status": "activated",
        "replacement_closed_reason": None,
        "replaces_job_id": job_a.id,
        "replaced_by_job_id": job_c.id,
    }
    assert projections[job_c.id] == {
        "replacement_id": relation_bc.id,
        "replacement_review_outcome": "pending",
        "replacement_lifecycle_status": "awaiting_review",
        "replacement_closed_reason": None,
        "replaces_job_id": job_b.id,
        "replaced_by_job_id": None,
    }

    filters = {
        "city": None,
        "district": None,
        "job_category": None,
        "pay_type": None,
        "audit_status": None,
        "delist_reason": None,
        "owner_userid": None,
        "created_from": None,
        "created_to": None,
        "expires_from": None,
        "expires_to": None,
        "salary_min": None,
        "salary_max": None,
    }
    admin = SimpleNamespace(username="reviewer")
    listed = jobs_api.list_jobs(
        **filters,
        lifecycle_scope="all",
        page=1,
        size=20,
        sort="id:asc",
        db=db,
        _=admin,
    )
    listed_by_id = {item["id"]: item for item in listed["data"]["items"]}
    assert listed_by_id[job_b.id]["replaces_job_id"] == job_a.id
    assert listed_by_id[job_b.id]["replaced_by_job_id"] == job_c.id
    assert listed_by_id[job_b.id]["replacement_id"] == relation_ab.id

    detail = jobs_api.get_job(job_b.id, db=db, _=admin)["data"]
    assert detail["replaces_job_id"] == job_a.id
    assert detail["replaced_by_job_id"] == job_c.id
    assert detail["replacement_id"] == relation_ab.id

    response = jobs_api.export_jobs(
        **filters,
        lifecycle_scope="all",
        sort="id:asc",
        db=db,
        _=admin,
    )
    exported = list(csv.DictReader(io.StringIO(response.body.decode("utf-8-sig"))))
    exported_by_id = {int(row["id"]): row for row in exported}
    assert exported_by_id[job_b.id]["replaces_job_id"] == str(job_a.id)
    assert exported_by_id[job_b.id]["replaced_by_job_id"] == str(job_c.id)
    assert exported_by_id[job_b.id]["replacement_id"] == str(relation_ab.id)


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
