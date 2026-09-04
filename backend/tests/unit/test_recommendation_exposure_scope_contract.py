"""Scope identity contract for recommendation_exposure_daily."""
from datetime import date, datetime
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.dialects.mysql import BIGINT, INTEGER
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.models import Base, RecommendationExposureDaily
from app.tasks.recommendation_exposure_reconcile import _upsert_batch


@compiles(BIGINT, "sqlite")
def _compile_bigint_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL hook
    return "INTEGER"


@compiles(INTEGER, "sqlite")
def _compile_integer_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL hook
    return "INTEGER"


def _db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[RecommendationExposureDaily.__table__])
    return engine, sessionmaker(bind=engine)()


def test_legacy_and_demo_same_candidate_day_can_coexist_on_sqlite():
    engine, db = _db()
    try:
        day = date(2026, 9, 4)
        db.add_all([
            RecommendationExposureDaily(
                stat_date=day, target_type="job", target_id=7,
                impression_count=2,
            ),
            RecommendationExposureDaily(
                stat_date=day, demo_id="demo-a", target_type="job", target_id=7,
                impression_count=3,
            ),
        ])
        db.commit()
        rows = db.query(RecommendationExposureDaily).order_by(
            RecommendationExposureDaily.scope_key,
        ).all()
        assert [(row.demo_id, row.scope_key, row.impression_count) for row in rows] == [
            (None, "", 2),
            ("demo-a", "demo-a", 3),
        ]
    finally:
        db.close()
        engine.dispose()


def test_demo_scope_upsert_is_idempotent_on_sqlite():
    engine, db = _db()
    try:
        stamp = datetime(2026, 9, 4, 12, 0, 0)
        _upsert_batch(db, date(2026, 9, 4), [("demo-a", "job", 7, 3)], stamp)
        _upsert_batch(db, date(2026, 9, 4), [("demo-a", "job", 7, 8)], stamp)
        rows = db.query(RecommendationExposureDaily).all()
        assert len(rows) == 1
        assert rows[0].demo_id == "demo-a"
        assert rows[0].scope_key == "demo-a"
        assert rows[0].impression_count == 8
    finally:
        db.close()
        engine.dispose()


def test_phase19_migration_has_guarded_scope_rekey_and_conflict_gate():
    migration = (
        Path(__file__).parents[2]
        / "sql"
        / "migrations"
        / "phase19_001_recommendation_exposure_scope.sql"
    ).read_text(encoding="utf-8").lower()
    assert "coalesce(`demo_id`, '')" in migration
    assert "group by `stat_date`, `target_type`, `target_id`, `scope_key" in migration
    assert "select * from phase19_scope_conflict_abort" in migration
    assert "drop primary key" in migration
    assert "add primary key (`stat_date`, `target_type`, `target_id`, `scope_key`)" in migration
    assert migration.count("prepare phase19_stmt from @ddl") >= 5
    # MySQL 8.0.45 rejects the previous form that embedded subqueries in a
    # dynamic IF expression.  Existence checks must be materialized first,
    # then only a single prepared statement is selected for execution.
    assert "select count(*) into @phase19_has_scope_key" in migration
    assert "select count(*) into @phase19_conflicts" in migration
    assert "select count(*) into @phase19_has_scope_constraint" in migration
    assert "set @ddl = if(" not in migration
