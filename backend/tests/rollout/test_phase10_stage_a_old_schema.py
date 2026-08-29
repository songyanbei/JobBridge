"""Smoke the immutable Phase 10 read-compat artifact against its old schema."""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pymysql
import pytest
from pymysql.constants import CLIENT
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


if os.getenv("RUN_PHASE10_STAGE_A") != "1":
    pytest.skip("run only against the Phase 10 Stage A artifact", allow_module_level=True)

from app.api.admin import audit as audit_api
from app.api.admin import jobs as jobs_api
import app
from app.db import engine
from app.models import Job, User
from app.services import audit_workbench_service


STAGE_A_ROOT = Path(os.environ["PHASE10_STAGE_A_ROOT"]).resolve()
OLD_SCHEMA_SQL = (STAGE_A_ROOT / "backend/sql/schema.sql").read_text(encoding="utf-8")
RELEASE_ROOT = Path(__file__).resolve().parents[2]
VISIBILITY_MIGRATIONS = tuple(
    (RELEASE_ROOT / "sql/migrations" / name).read_text(encoding="utf-8")
    for name in (
        "phase10_001_job_visibility_fields.sql",
        "phase10_002_ensure_visibility_config.sql",
    )
)


def _connect(database: str | None = None):
    url = engine.url
    return pymysql.connect(
        host=url.host or "127.0.0.1",
        port=int(url.port or 3306),
        user=url.username,
        password=url.password,
        database=database,
        charset="utf8mb4",
        autocommit=True,
        client_flag=CLIENT.MULTI_STATEMENTS,
    )


def _job_filters() -> dict:
    return {
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


@pytest.mark.parametrize(
    "apply_visibility",
    [False, True],
    ids=["base-old-schema", "visibility-expanded-schema"],
)
def test_stage_a_reads_pre_lifecycle_schemas(monkeypatch, apply_visibility):
    assert Path(app.__file__).resolve().is_relative_to(STAGE_A_ROOT / "backend")
    database = f"phase10_stage_a_{uuid4().hex[:16]}"
    admin = _connect()
    stage_engine = None
    db = None
    try:
        with admin.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4")
        schema_db = _connect(database)
        try:
            with schema_db.cursor() as cursor:
                cursor.execute(OLD_SCHEMA_SQL)
                while cursor.nextset():
                    pass
                if apply_visibility:
                    for migration in VISIBILITY_MIGRATIONS:
                        cursor.execute(migration)
                        while cursor.nextset():
                            pass
        finally:
            schema_db.close()

        stage_engine = create_engine(engine.url.set(database=database), pool_pre_ping=True)
        db = sessionmaker(bind=stage_engine, autoflush=False, autocommit=False)()
        owner_userid = f"stage-a-owner-{uuid4().hex}"
        db.add(User(external_userid=owner_userid, role="factory"))
        db.flush()
        jobs = []
        for index in range(2):
            job = Job(
                owner_userid=owner_userid,
                city="苏州市",
                job_category="电子厂",
                salary_floor_monthly=5500 + index * 100,
                pay_type="月薪",
                headcount=10,
                gender_required="不限",
                is_long_term=True,
                raw_text=f"阶段 A 旧结构冒烟岗位 {index + 1}",
                description=f"阶段 A 旧结构冒烟岗位 {index + 1}",
                audit_status="pending",
                expires_at=datetime.now() + timedelta(days=30),
                created_at=datetime.now() - timedelta(minutes=index),
                version=1,
            )
            jobs.append(job)
            db.add(job)
        db.commit()
        for job in jobs:
            db.refresh(job)

        filters = _job_filters()
        first_page = jobs_api.list_jobs(
            **filters, page=1, size=1, sort="created_at:desc", db=db, _=None
        )
        second_page = jobs_api.list_jobs(
            **filters, page=2, size=1, sort="created_at:desc", db=db, _=None
        )
        assert first_page["data"]["total"] == 2
        assert second_page["data"]["total"] == 2
        assert len(first_page["data"]["items"]) == 1
        assert len(second_page["data"]["items"]) == 1
        assert (
            first_page["data"]["items"][0]["id"]
            != second_page["data"]["items"][0]["id"]
        )

        detail = jobs_api.get_job(jobs[0].id, db=db, _=None)
        assert detail["data"]["id"] == jobs[0].id

        exported = jobs_api.export_jobs(
            **filters, sort="created_at:desc", db=db, _=None
        )
        assert exported.status_code == 200
        assert owner_userid.encode() in bytes(exported.body)

        monkeypatch.setattr(
            audit_workbench_service,
            "get_audit_lock_holder",
            lambda *_args, **_kwargs: None,
        )
        queue = audit_api.queue(
            status="pending", target_type="job", page=1, size=20, db=db, _=None
        )
        assert queue["data"]["total"] == 2
        audit_detail = audit_api.detail("job", jobs[0].id, db=db, _=None)
        assert audit_detail["data"]["id"] == jobs[0].id

        with db.connection().connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='job' "
                "AND COLUMN_NAME IN ('activated_at','candidate_expires_at')"
            )
            assert int(cursor.fetchone()[0]) == 0
            cursor.execute(
                "SELECT COUNT(*) FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='job' "
                "AND COLUMN_NAME IN ('hiring_company','contact_person','phone')"
            )
            assert int(cursor.fetchone()[0]) == (3 if apply_visibility else 0)
    finally:
        if db is not None:
            db.close()
        if stage_engine is not None:
            stage_engine.dispose()
        with admin.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")
        admin.close()
