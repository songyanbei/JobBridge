"""Search SQL integration tests that require a real MySQL database."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import sqlalchemy as sa

import app.models  # noqa: F401  - registers ORM metadata for create_all
from app.db import Base, SessionLocal, engine
from app.models import Job, Resume, User
from app.services.search_service import _query_jobs

pytestmark = pytest.mark.integration


def _future() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=7)


def _past() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=1)


@pytest.fixture(scope="module", autouse=True)
def _mysql_schema():
    Base.metadata.create_all(bind=engine)


@pytest.fixture()
def db_session():
    db = SessionLocal()
    prefix = f"search-sql-{uuid4().hex[:12]}"
    try:
        yield db, prefix
    finally:
        db.rollback()
        _delete_test_rows(db, prefix)
        db.close()


def _delete_test_rows(db, prefix: str) -> None:
    userids = (
        sa.select(User.external_userid)
        .where(User.external_userid.like(f"{prefix}%"))
        .subquery()
    )
    db.query(Job).filter(Job.owner_userid.in_(sa.select(userids.c.external_userid))).delete(
        synchronize_session=False,
    )
    db.query(Resume).filter(Resume.owner_userid.in_(sa.select(userids.c.external_userid))).delete(
        synchronize_session=False,
    )
    db.query(User).filter(User.external_userid.like(f"{prefix}%")).delete(
        synchronize_session=False,
    )
    db.commit()


def _user(prefix: str, suffix: str, *, status: str = "active", role: str = "factory") -> User:
    return User(
        external_userid=f"{prefix}-{suffix}",
        role=role,
        status=status,
        display_name=f"test {suffix}",
        can_search_jobs=role in {"worker", "broker"},
        can_search_workers=role in {"factory", "broker"},
    )


def _job(owner_userid: str, **overrides) -> Job:
    values = {
        "owner_userid": owner_userid,
        "city": "苏州市",
        "job_category": "电子厂",
        "salary_floor_monthly": 6000,
        "salary_ceiling_monthly": 8000,
        "pay_type": "月薪",
        "headcount": 3,
        "raw_text": "苏州电子厂招聘",
        "description": "苏州电子厂招聘",
        "audit_status": "passed",
        "expires_at": _future(),
        "deleted_at": None,
        "delist_reason": None,
    }
    values.update(overrides)
    return Job(**values)


def _resume(owner_userid: str, **overrides) -> Resume:
    values = {
        "owner_userid": owner_userid,
        "expected_cities": ["苏州市"],
        "expected_job_categories": ["电子厂"],
        "salary_expect_floor_monthly": 5500,
        "gender": "男",
        "age": 30,
        "raw_text": "找苏州电子厂工作",
        "description": "找苏州电子厂工作",
        "audit_status": "passed",
        "expires_at": _future(),
        "deleted_at": None,
    }
    values.update(overrides)
    return Resume(**values)


def test_mysql_fixture_can_insert_search_entities(db_session):
    db, prefix = db_session
    factory = _user(prefix, "factory")
    worker = _user(prefix, "worker", role="worker")
    db.add_all([factory, worker])
    db.flush()
    db.add_all([_job(factory.external_userid), _resume(worker.external_userid)])
    db.commit()

    assert db.query(User).filter(User.external_userid.like(f"{prefix}%")).count() == 2
    assert db.query(Job).filter(Job.owner_userid == factory.external_userid).count() == 1
    assert db.query(Resume).filter(Resume.owner_userid == worker.external_userid).count() == 1


def test_query_jobs_runs_against_mysql_and_filters_lifecycle(db_session):
    db, prefix = db_session
    active_factory = _user(prefix, "active-factory")
    inactive_factory = _user(prefix, "inactive-factory", status="blocked")
    db.add_all([active_factory, inactive_factory])
    db.flush()

    wanted = _job(active_factory.external_userid, raw_text="wanted", description="wanted")
    db.add_all([
        wanted,
        _job(active_factory.external_userid, audit_status="pending"),
        _job(active_factory.external_userid, expires_at=_past()),
        _job(active_factory.external_userid, deleted_at=datetime.now(timezone.utc)),
        _job(inactive_factory.external_userid),
    ])
    db.commit()

    rows = _query_jobs({"city": ["苏州市"], "job_category": ["电子厂"]}, 20, db)

    assert [row.id for row in rows] == [wanted.id]
