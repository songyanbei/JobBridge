from datetime import datetime, timezone
import json
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.models import Job, TargetCleanupTask, User
from scripts import backfill_target_cleanup_tasks


@compiles(mysql.TINYINT, "sqlite")
@compiles(mysql.SMALLINT, "sqlite")
@compiles(mysql.INTEGER, "sqlite")
@compiles(mysql.BIGINT, "sqlite")
def _compile_mysql_integer_for_sqlite(_type, _compiler, **_kwargs):
    return "INTEGER"


@pytest.fixture()
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    for table in (User.__table__, Job.__table__, TargetCleanupTask.__table__):
        table.create(engine)
    factory = sessionmaker(bind=engine)
    try:
        yield factory
    finally:
        engine.dispose()


def test_backfill_reports_actual_created_and_remaining_missing(
    session_factory, monkeypatch,
):
    with session_factory() as db:
        db.add(User(external_userid="owner", role="factory"))
        db.add_all([
            Job(
                id=job_id,
                owner_userid="owner",
                city="N01",
                job_category="N01",
                salary_floor_monthly=5000,
                pay_type=Job.__table__.c.pay_type.type.enums[0],
                headcount=1,
                raw_text="target cleanup backfill",
                deleted_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            for job_id in (1, 2)
        ])
        db.add(TargetCleanupTask(
            operation_id="existing-task",
            target_type="job",
            target_id=1,
            reason="historical_soft_delete",
            reason_history=["historical_soft_delete"],
            status="pending",
        ))
        db.commit()

    monkeypatch.setattr(
        backfill_target_cleanup_tasks, "SessionLocal", session_factory,
    )

    assert backfill_target_cleanup_tasks.run(apply=False, batch_size=1) == {
        "scanned": 2,
        "created": 0,
        "missing": 1,
    }
    assert backfill_target_cleanup_tasks.run(apply=True, batch_size=1) == {
        "scanned": 2,
        "created": 1,
        "missing": 0,
    }
    assert backfill_target_cleanup_tasks.run(apply=True, batch_size=1) == {
        "scanned": 2,
        "created": 0,
        "missing": 0,
    }


def test_backfill_does_not_count_task_created_by_racing_writer(
    session_factory, monkeypatch,
):
    with session_factory() as db:
        db.add(User(external_userid="race-owner", role="factory"))
        db.add(Job(
            id=3,
            owner_userid="race-owner",
            city="N01",
            job_category="N01",
            salary_floor_monthly=5000,
            pay_type=Job.__table__.c.pay_type.type.enums[0],
            headcount=1,
            raw_text="target cleanup backfill race",
            deleted_at=datetime.now(timezone.utc).replace(tzinfo=None),
        ))
        db.commit()

    def _racing_winner(db, job_id, *, reason, operation_id=None):
        task = TargetCleanupTask(
            operation_id=operation_id or "racing-winner",
            target_type="job",
            target_id=job_id,
            reason=reason,
            reason_history=[reason],
            status="pending",
        )
        db.add(task)
        db.flush()
        return task, False

    monkeypatch.setattr(
        backfill_target_cleanup_tasks, "SessionLocal", session_factory,
    )
    monkeypatch.setattr(
        backfill_target_cleanup_tasks,
        "upsert_job_cleanup_task",
        _racing_winner,
    )

    assert backfill_target_cleanup_tasks.run(apply=True, batch_size=1) == {
        "scanned": 1,
        "created": 0,
        "missing": 0,
    }


def test_backfill_rechecks_global_coverage_after_keyset_scan(
    session_factory, monkeypatch,
):
    with session_factory() as db:
        db.add(User(external_userid="late-owner", role="factory"))
        db.add_all([
            Job(
                id=job_id,
                owner_userid="late-owner",
                city="N17",
                job_category="N17",
                salary_floor_monthly=5000,
                pay_type=Job.__table__.c.pay_type.type.enums[0],
                headcount=1,
                raw_text="target cleanup final coverage",
                deleted_at=(
                    None if job_id == 1
                    else datetime.now(timezone.utc).replace(tzinfo=None)
                ),
            )
            for job_id in (1, 2)
        ])
        db.commit()

    real_upsert = backfill_target_cleanup_tasks.upsert_job_cleanup_task

    def _soft_delete_behind_cursor(db, job_id, **kwargs):
        db.get(Job, 1).deleted_at = datetime.now(timezone.utc).replace(tzinfo=None)
        return real_upsert(db, job_id, **kwargs)

    monkeypatch.setattr(
        backfill_target_cleanup_tasks, "SessionLocal", session_factory,
    )
    monkeypatch.setattr(
        backfill_target_cleanup_tasks,
        "upsert_job_cleanup_task",
        _soft_delete_behind_cursor,
    )

    assert backfill_target_cleanup_tasks.run(apply=True, batch_size=1) == {
        "scanned": 1,
        "created": 1,
        "missing": 1,
    }


def test_backfill_rejects_non_positive_batch_size():
    with pytest.raises(ValueError, match="batch_size must be positive"):
        backfill_target_cleanup_tasks.run(apply=False, batch_size=0)


@pytest.mark.parametrize(
    ("argv", "result", "expected_apply", "expected_batch", "expected_exit", "mode"),
    [
        (
            ["backfill_target_cleanup_tasks"],
            {"scanned": 2, "created": 0, "missing": 1},
            False,
            500,
            1,
            "dry-run",
        ),
        (
            ["backfill_target_cleanup_tasks", "--apply", "--batch-size", "17"],
            {"scanned": 2, "created": 1, "missing": 0},
            True,
            17,
            0,
            "apply",
        ),
    ],
)
def test_cli_mode_arguments_and_missing_exit_code(
    monkeypatch,
    capsys,
    argv,
    result,
    expected_apply,
    expected_batch,
    expected_exit,
    mode,
):
    calls = []

    def _run(*, apply, batch_size):
        calls.append((apply, batch_size))
        return result

    monkeypatch.setattr(backfill_target_cleanup_tasks, "run", _run)
    monkeypatch.setattr(sys, "argv", argv)

    assert backfill_target_cleanup_tasks.main() == expected_exit
    assert calls == [(expected_apply, expected_batch)]
    assert json.loads(capsys.readouterr().out) == {"mode": mode, **result}
