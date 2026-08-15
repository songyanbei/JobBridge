"""推荐域延迟硬删任务单测（方案 §9.11.1 / §10.1.1 行 2240）。

``due_userids`` 的 SQL 用了 MySQL 专有函数，只在集成环境验证；这里覆盖任务编排：
锁、逐用户隔离、失败进重试队列、重试重放。
"""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.recommendation_privacy_service import PrivacyReport
from app.tasks import recommendation_privacy_cleanup as task


@pytest.fixture(autouse=True)
def _patch_infra(monkeypatch):
    """屏蔽真实 Redis 锁与 DB session。"""
    @contextmanager
    def _lock(name, ttl=3600):
        yield True

    @contextmanager
    def _session():
        yield MagicMock()

    monkeypatch.setattr(task, "task_lock", _lock)
    monkeypatch.setattr(task, "SessionLocal", _session)
    monkeypatch.setattr(task.privacy, "privacy_retry_depth", lambda: {"pending": 0})


def _report(*, ok: bool = True, rows: int = 3) -> PrivacyReport:
    report = PrivacyReport(batch_id="b1")
    report.add("viewer_request", rows)
    if not ok:
        report.failed_steps.append("delete_viewer_facts")
    return report


class TestRun:
    def test_skips_when_lock_is_held(self, monkeypatch):
        @contextmanager
        def _busy(name, ttl=3600):
            yield False

        monkeypatch.setattr(task, "task_lock", _busy)
        monkeypatch.setattr(task, "due_userids", lambda *a, **k: pytest.fail("不该扫描"))

        task.run()

    def test_processes_every_due_user(self, monkeypatch):
        calls: list[str] = []
        monkeypatch.setattr(task, "_read_delay_days", lambda db: 7)
        monkeypatch.setattr(task, "due_userids", lambda db, delay, limit=200: ["u1", "u2"])
        monkeypatch.setattr(task, "drain_retry_queue", lambda db, limit=100: {"replayed": 0})

        def _delete(db, userid, **kwargs):
            calls.append(userid)
            return _report()

        monkeypatch.setattr(task.privacy, "delete_recommendation_user_data", _delete)

        task.run()

        assert calls == ["u1", "u2"]

    def test_one_failure_does_not_block_other_users(self, monkeypatch):
        calls: list[str] = []
        enqueued: list[tuple] = []
        monkeypatch.setattr(task, "_read_delay_days", lambda db: 7)
        monkeypatch.setattr(task, "due_userids", lambda db, delay, limit=200: ["u1", "u2"])
        monkeypatch.setattr(task, "drain_retry_queue", lambda db, limit=100: {"replayed": 0})

        def _delete(db, userid, **kwargs):
            calls.append(userid)
            if userid == "u1":
                raise RuntimeError("boom")
            return _report()

        monkeypatch.setattr(task.privacy, "delete_recommendation_user_data", _delete)
        monkeypatch.setattr(
            task.privacy, "enqueue_privacy_retry",
            lambda userid, **kw: enqueued.append((userid, kw.get("failed_steps"))) or True,
        )

        task.run()

        assert calls == ["u1", "u2"]
        assert enqueued == [("u1", ["closure"])]

    def test_partial_failure_goes_to_retry_queue(self, monkeypatch):
        enqueued: list[str] = []
        monkeypatch.setattr(task, "_read_delay_days", lambda db: 7)
        monkeypatch.setattr(task, "due_userids", lambda db, delay, limit=200: ["u1"])
        monkeypatch.setattr(task, "drain_retry_queue", lambda db, limit=100: {"replayed": 0})
        monkeypatch.setattr(
            task.privacy, "delete_recommendation_user_data",
            lambda db, userid, **kw: _report(ok=False),
        )
        monkeypatch.setattr(
            task.privacy, "enqueue_privacy_retry",
            lambda userid, **kw: enqueued.append(userid) or True,
        )

        task.run()

        assert enqueued == ["u1"]


class TestDrainRetryQueue:
    def test_replays_until_queue_is_empty(self, monkeypatch):
        jobs = [
            {"userid": "u1", "attempt": 1},
            {"userid": "u2", "attempt": 2},
        ]
        monkeypatch.setattr(
            task.privacy, "pop_privacy_retry", lambda: jobs.pop(0) if jobs else None,
        )
        replayed: list[str] = []
        monkeypatch.setattr(
            task.privacy, "delete_recommendation_user_data",
            lambda db, userid, **kw: replayed.append(userid) or _report(),
        )

        stats = task.drain_retry_queue(MagicMock())

        assert replayed == ["u1", "u2"]
        assert stats == {"replayed": 2, "recovered": 2}

    def test_still_failing_job_is_requeued_with_next_attempt(self, monkeypatch):
        jobs = [{"userid": "u1", "attempt": 3}]
        attempts: list[int] = []
        monkeypatch.setattr(
            task.privacy, "pop_privacy_retry", lambda: jobs.pop(0) if jobs else None,
        )
        monkeypatch.setattr(
            task.privacy, "delete_recommendation_user_data",
            lambda db, userid, **kw: _report(ok=False),
        )
        monkeypatch.setattr(
            task.privacy, "enqueue_privacy_retry",
            lambda userid, **kw: attempts.append(kw.get("attempt")) or True,
        )

        stats = task.drain_retry_queue(MagicMock())

        assert stats == {"replayed": 1, "recovered": 0}
        assert attempts == [3]

    def test_ignores_malformed_payload(self, monkeypatch):
        jobs = [{"nope": 1}]
        monkeypatch.setattr(
            task.privacy, "pop_privacy_retry", lambda: jobs.pop(0) if jobs else None,
        )
        monkeypatch.setattr(
            task.privacy, "delete_recommendation_user_data",
            lambda db, userid, **kw: pytest.fail("payload 缺 userid 不该重放"),
        )

        assert task.drain_retry_queue(MagicMock()) == {"replayed": 0, "recovered": 0}


class TestDelayConfig:
    def test_falls_back_when_config_missing(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        assert task._read_delay_days(db) == task.DEFAULT_DELAY_DAYS

    def test_falls_back_when_config_is_not_an_int(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = (
            SimpleNamespace(config_value="abc")
        )
        assert task._read_delay_days(db) == task.DEFAULT_DELAY_DAYS

    def test_reads_shared_hard_delete_delay(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = (
            SimpleNamespace(config_value="14")
        )
        assert task._read_delay_days(db) == 14
