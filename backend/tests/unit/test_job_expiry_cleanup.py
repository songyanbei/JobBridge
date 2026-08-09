from __future__ import annotations

from unittest.mock import MagicMock

from app.tasks import job_expiry_cleanup


def _result(*, rows=None, rowcount=0):
    result = MagicMock()
    result.fetchall.return_value = rows or []
    result.rowcount = rowcount
    return result


def test_locked_batch_is_ordered_skip_locked_and_conditionally_produces_cleanup(monkeypatch):
    db = MagicMock()
    db.execute.side_effect = [
        _result(rows=[(11,), (12,)]),
        _result(rowcount=1),
        _result(rowcount=0),
    ]
    ensure = MagicMock()
    monkeypatch.setattr(job_expiry_cleanup, "ensure_job_cleanup_task", ensure)

    expired = job_expiry_cleanup.expire_locked_batch(
        db, now=job_expiry_cleanup._utcnow(), batch_size=500
    )

    assert expired == [11]
    select_sql = str(db.execute.call_args_list[0].args[0])
    assert "ORDER BY expires_at ASC, id ASC" in select_sql
    assert "FOR UPDATE SKIP LOCKED" in select_sql
    update_sql = str(db.execute.call_args_list[1].args[0])
    assert "expires_at <= :now" in update_sql
    assert "deleted_at IS NULL" in update_sql
    assert "delist_reason IS NULL" in update_sql
    assert "version=version+1" in update_sql
    ensure.assert_called_once_with(db, 11, reason="expired")
    db.query.return_value.filter.return_value.update.assert_called_once()
    db.commit.assert_called_once()


def test_processor_drains_batches_and_renews_after_each_commit(monkeypatch):
    expire = MagicMock(side_effect=[[1, 2], [3], []])
    lease = MagicMock()
    lease.renew.return_value = True
    monkeypatch.setattr(job_expiry_cleanup, "expire_locked_batch", expire)

    stats = job_expiry_cleanup.process_expired_jobs(
        MagicMock(), batch_size=2, max_runtime_seconds=None, lease=lease
    )

    assert stats == {
        "processed": 3,
        "batches": 2,
        "continuation_scheduled": False,
    }
    assert lease.renew.call_count == 2


def test_processor_schedules_immediate_continuation_at_runtime_limit(monkeypatch):
    monkeypatch.setattr(job_expiry_cleanup.time, "monotonic", MagicMock(side_effect=[0, 481]))
    expire = MagicMock()
    continuation = MagicMock()
    monkeypatch.setattr(job_expiry_cleanup, "expire_locked_batch", expire)

    stats = job_expiry_cleanup.process_expired_jobs(
        MagicMock(), max_runtime_seconds=480, continuation=continuation
    )

    assert stats["continuation_scheduled"] is True
    continuation.assert_called_once_with()
    expire.assert_not_called()


def test_processor_stops_when_lease_is_lost(monkeypatch):
    expire = MagicMock(return_value=[1])
    lease = MagicMock()
    lease.renew.return_value = False
    monkeypatch.setattr(job_expiry_cleanup, "expire_locked_batch", expire)

    stats = job_expiry_cleanup.process_expired_jobs(
        MagicMock(), max_runtime_seconds=None, lease=lease
    )

    assert stats["processed"] == 1
    expire.assert_called_once()


def test_run_honors_expiry_feature_switch(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "job_expiry_cleanup_enabled", False)
    lock = MagicMock()
    monkeypatch.setattr(job_expiry_cleanup, "renewable_task_lock", lock)

    job_expiry_cleanup.run()

    lock.assert_not_called()
