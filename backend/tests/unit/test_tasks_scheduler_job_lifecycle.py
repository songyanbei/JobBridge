from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from apscheduler.triggers.date import DateTrigger

from app.tasks import scheduler


def test_scheduler_registers_ten_minute_job_lifecycle_tasks():
    sched = scheduler.build_scheduler()
    expiry = sched.get_job("job_expiry_cleanup")
    candidate = sched.get_job("job_candidate_cleanup")
    assert expiry is not None and candidate is not None
    assert expiry.trigger.interval.total_seconds() == 600
    assert candidate.trigger.interval.total_seconds() == 600


def test_scheduler_registers_media_dead_letter_monitor():
    sched = scheduler.build_scheduler()
    monitor = sched.get_job("media_cleanup_health")
    assert monitor is not None
    assert monitor.trigger.interval.total_seconds() == 60


def test_expiry_continuation_is_one_shot_five_seconds_later(monkeypatch):
    fake_scheduler = MagicMock()
    monkeypatch.setattr(scheduler, "_scheduler", fake_scheduler)
    before = datetime.now(timezone.utc)

    assert scheduler.schedule_job_expiry_continuation() is True

    kwargs = fake_scheduler.add_job.call_args.kwargs
    assert kwargs["id"] == "job_expiry_cleanup_continuation"
    assert kwargs["replace_existing"] is True
    assert isinstance(fake_scheduler.add_job.call_args.args[1], DateTrigger)
    run_date = fake_scheduler.add_job.call_args.args[1].run_date
    assert 4 <= (run_date - before).total_seconds() <= 6


def test_candidate_continuation_is_one_shot(monkeypatch):
    fake_scheduler = MagicMock()
    monkeypatch.setattr(scheduler, "_scheduler", fake_scheduler)

    assert scheduler.schedule_job_candidate_continuation() is True

    kwargs = fake_scheduler.add_job.call_args.kwargs
    assert kwargs["id"] == "job_candidate_cleanup_continuation"
    assert kwargs["replace_existing"] is True
