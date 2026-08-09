"""Phase 7 tasks/ttl_cleanup.py 单元测试。

验证：
- ``_load_ttl_config`` 在 DB 缺失/有值/非法值三种场景下的回退行为
- ``_safe_step`` 捕获异常并写 -1 而不影响其它步骤
- ``_batch_hard_delete`` 分批 DELETE LIMIT 500，最后一批 < 500 时退出
- ``_escape_literal`` 防御性转义单引号 / 反斜杠
- ``run()`` 未获取锁时直接 return，不读 DB
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from app.tasks import ttl_cleanup


# ---------------------------------------------------------------------------
# _load_ttl_config / _read_int_config
# ---------------------------------------------------------------------------

def _stub_db_with_config(values: dict[str, str | None]) -> MagicMock:
    """构造一个 MagicMock db，db.execute(text(...), {'k': key}).first() 按 ``values`` 返回。

    values 中没有的 key → first() 返回 None（模拟 row not found）。
    """
    db = MagicMock()

    def _execute(_stmt, params=None):
        params = params or {}
        result = MagicMock()
        key = params.get("k")
        val = values.get(key)
        if val is None:
            result.first.return_value = None
        else:
            result.first.return_value = (val,)
        return result

    db.execute.side_effect = _execute
    return db


class TestLoadTtlConfig:
    def test_all_defaults_when_db_empty(self):
        db = _stub_db_with_config({})
        cfg = ttl_cleanup._load_ttl_config(db)
        assert cfg == {
            "hard_delete_delay_days": 7,
            "conversation_log_days": 30,
            "wecom_inbound_event_days": 30,
            "audit_log_days": 180,
        }

    def test_reads_custom_values(self):
        db = _stub_db_with_config({
            "ttl.hard_delete.delay_days": "14",
            "ttl.conversation_log.days": "60",
            "ttl.wecom_inbound_event.days": "45",
            "ttl.audit_log.days": "365",
        })
        cfg = ttl_cleanup._load_ttl_config(db)
        assert cfg["hard_delete_delay_days"] == 14
        assert cfg["conversation_log_days"] == 60
        assert cfg["wecom_inbound_event_days"] == 45
        assert cfg["audit_log_days"] == 365

    def test_invalid_int_falls_back_to_default(self):
        """运营把值改成"abc"等非数字时，单 key 退回默认，不影响其它 key。"""
        db = _stub_db_with_config({
            "ttl.hard_delete.delay_days": "not-a-number",
            "ttl.conversation_log.days": "60",
        })
        cfg = ttl_cleanup._load_ttl_config(db)
        assert cfg["hard_delete_delay_days"] == 7   # 默认
        assert cfg["conversation_log_days"] == 60   # 自定义

    def test_partial_keys_missing_uses_per_key_defaults(self):
        db = _stub_db_with_config({"ttl.audit_log.days": "90"})
        cfg = ttl_cleanup._load_ttl_config(db)
        assert cfg["audit_log_days"] == 90
        assert cfg["hard_delete_delay_days"] == 7
        assert cfg["conversation_log_days"] == 30
        assert cfg["wecom_inbound_event_days"] == 30


# ---------------------------------------------------------------------------
# _safe_step
# ---------------------------------------------------------------------------

class TestSafeStep:
    def test_records_return_value_on_success(self):
        stats: dict = {}
        ttl_cleanup._safe_step("foo", stats, lambda: 42)
        assert stats == {"foo": 42}

    def test_records_minus_one_on_exception(self):
        stats: dict = {}

        def boom():
            raise RuntimeError("simulated step failure")

        ttl_cleanup._safe_step("bar", stats, boom)
        assert stats == {"bar": -1}

    def test_one_step_failure_does_not_break_subsequent_steps(self):
        stats: dict = {}
        ttl_cleanup._safe_step("ok1", stats, lambda: 1)
        ttl_cleanup._safe_step("fail", stats, lambda: (_ for _ in ()).throw(ValueError("x")))
        ttl_cleanup._safe_step("ok2", stats, lambda: 3)
        assert stats == {"ok1": 1, "fail": -1, "ok2": 3}


# ---------------------------------------------------------------------------
# _batch_hard_delete 分批
# ---------------------------------------------------------------------------

class TestBatchHardDelete:
    def test_single_batch_under_size_terminates(self):
        db = MagicMock()
        result = MagicMock()
        result.rowcount = 100  # < BATCH_SIZE (500)
        db.execute.return_value = result

        total = ttl_cleanup._batch_hard_delete(db, "resume", "deleted_at IS NOT NULL")
        assert total == 100
        # 只执行一次 DELETE 就退出
        assert db.execute.call_count == 1
        db.commit.assert_called_once()

    def test_multi_batch_loops_until_under_size(self):
        db = MagicMock()
        results = [MagicMock(rowcount=500), MagicMock(rowcount=500), MagicMock(rowcount=37)]
        db.execute.side_effect = results

        total = ttl_cleanup._batch_hard_delete(db, "audit_log", "created_at < NOW()")
        assert total == 500 + 500 + 37
        assert db.execute.call_count == 3
        assert db.commit.call_count == 3

    def test_zero_rows_terminates_immediately(self):
        db = MagicMock()
        result = MagicMock()
        result.rowcount = 0
        db.execute.return_value = result

        assert ttl_cleanup._batch_hard_delete(db, "resume", "1=0") == 0
        assert db.execute.call_count == 1


def test_inbound_cleanup_preserves_recovery_and_unsent_outbox(monkeypatch):
    captured = {}

    def fake_delete(db, table, where):
        captured.update(table=table, where=where)
        return 7

    monkeypatch.setattr(ttl_cleanup, "_batch_hard_delete", fake_delete)

    assert ttl_cleanup._hard_delete_terminal_inbound(MagicMock(), 30) == 7
    assert captured["table"] == "wecom_inbound_event"
    where = captured["where"]
    assert "status IN ('done','dead_letter')" in where
    assert "session_pending" not in where
    assert "NOT EXISTS" in where
    assert "o.status IN ('pending','sending')" in where


def test_deleted_user_cleanup_includes_only_terminal_inbound(monkeypatch):
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = [("u1",)]
    calls = []

    def fake_delete(_db, table, where):
        calls.append((table, where))
        return 1

    monkeypatch.setattr(ttl_cleanup, "_batch_hard_delete", fake_delete)
    monkeypatch.setattr(ttl_cleanup, "log_event", MagicMock())

    assert ttl_cleanup._hard_delete_deleted_users(db, 7) == 3
    assert all(table != "resume" for table, _where in calls)
    inbound = [where for table, where in calls if table == "wecom_inbound_event"]
    assert len(inbound) == 1
    assert "from_userid = 'u1'" in inbound[0]
    assert "status IN ('done','dead_letter')" in inbound[0]


# ---------------------------------------------------------------------------
# _escape_literal
# ---------------------------------------------------------------------------

class TestEscapeLiteral:
    def test_normal_userid(self):
        assert ttl_cleanup._escape_literal("UserABC") == "'UserABC'"

    def test_single_quote_escaped(self):
        assert ttl_cleanup._escape_literal("a'b") == "'a''b'"

    def test_backslash_escaped(self):
        assert ttl_cleanup._escape_literal("a\\b") == "'a\\\\b'"


# ---------------------------------------------------------------------------
# run() lock 行为
# ---------------------------------------------------------------------------

class TestRunLock:
    def test_skip_when_lock_not_acquired(self):
        """未拿到分布式锁时直接 return，不打开 DB session。"""
        with patch.object(ttl_cleanup, "task_lock") as mock_lock, \
             patch.object(ttl_cleanup, "SessionLocal") as mock_session:
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=False)  # acquired=False
            cm.__exit__ = MagicMock(return_value=False)
            mock_lock.return_value = cm

            ttl_cleanup.run()

            mock_session.assert_not_called()


def test_daily_job_soft_delete_delegates_to_lock_safe_processor(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "job_expiry_cleanup_enabled", True)
    processor = MagicMock(return_value={"processed": 17})
    monkeypatch.setattr(
        "app.tasks.job_expiry_cleanup.process_expired_jobs", processor
    )
    db = MagicMock()

    assert ttl_cleanup._soft_delete_expired_jobs(db) == 17
    processor.assert_called_once_with(db, max_runtime_seconds=None)


def test_daily_job_soft_delete_honors_disabled_rollout_gate(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "job_expiry_cleanup_enabled", False)
    processor = MagicMock()
    monkeypatch.setattr(
        "app.tasks.job_expiry_cleanup.process_expired_jobs", processor
    )

    assert ttl_cleanup._soft_delete_expired_jobs(MagicMock()) == 0
    processor.assert_not_called()


def _hard_delete_db(*, active_relation=False):
    db = MagicMock()

    def execute(statement, _params=None):
        sql = str(statement)
        result = MagicMock()
        if sql.lstrip().startswith("SELECT id, images, deleted_at"):
            result.fetchall.return_value = [(7, '["images/a.jpg"]', datetime(2026, 1, 1))]
        elif sql.lstrip().startswith("DELETE FROM `job`"):
            result.rowcount = 1
        else:
            raise AssertionError(f"unexpected SQL: {sql}")
        return result

    db.execute.side_effect = execute
    db.query.return_value.filter.return_value.first.return_value = (
        (1,) if active_relation else None
    )
    return db


def test_job_hard_delete_feature_switch_is_fail_closed(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "job_hard_delete_enabled", False)
    db = MagicMock()
    assert ttl_cleanup._hard_delete_expired_jobs(db, 7) == 0
    db.execute.assert_not_called()


def test_job_hard_delete_blocks_active_replacement(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "job_hard_delete_enabled", True)
    db = _hard_delete_db(active_relation=True)
    assert ttl_cleanup._hard_delete_expired_jobs(db, 7) == 0
    assert not any(
        str(call.args[0]).lstrip().startswith("DELETE FROM `job`")
        for call in db.execute.call_args_list
    )


@pytest.mark.parametrize(
    "media_marked,cleanup_succeeded,media_complete",
    [(1, True, True), (0, False, True), (0, True, False)],
)
def test_job_hard_delete_blocks_each_cleanup_gate(
    monkeypatch, media_marked, cleanup_succeeded, media_complete,
):
    from app.config import settings
    from app.services import job_media_service, target_cleanup_service

    monkeypatch.setattr(settings, "job_hard_delete_enabled", True)
    monkeypatch.setattr(
        job_media_service, "mark_job_media_delete_pending", lambda *_: media_marked
    )
    monkeypatch.setattr(
        target_cleanup_service, "job_cleanup_succeeded", lambda *_: cleanup_succeeded
    )
    monkeypatch.setattr(
        job_media_service, "hard_delete_media_complete", lambda *_: media_complete
    )
    db = _hard_delete_db()

    assert ttl_cleanup._hard_delete_expired_jobs(db, 7) == 0
    assert not any(
        str(call.args[0]).lstrip().startswith("DELETE FROM `job`")
        for call in db.execute.call_args_list
    )


def test_job_hard_delete_rechecks_cleanup_and_replacement_in_delete(monkeypatch):
    from app.config import settings
    from app.services import job_media_service, target_cleanup_service

    monkeypatch.setattr(settings, "job_hard_delete_enabled", True)
    monkeypatch.setattr(job_media_service, "mark_job_media_delete_pending", lambda *_: 0)
    monkeypatch.setattr(target_cleanup_service, "job_cleanup_succeeded", lambda *_: True)
    monkeypatch.setattr(job_media_service, "hard_delete_media_complete", lambda *_: True)
    db = _hard_delete_db()

    assert ttl_cleanup._hard_delete_expired_jobs(db, 7) == 1
    delete_sql = next(
        str(call.args[0]) for call in db.execute.call_args_list
        if str(call.args[0]).lstrip().startswith("DELETE FROM `job`")
    )
    assert "target_cleanup_task" in delete_sql
    assert "t.status='succeeded'" in delete_sql
    assert "NOT EXISTS" in delete_sql
    assert "media_asset_lifecycle" in delete_sql
    assert "m.state<>'deleted'" in delete_sql
    assert "job_replacement" in delete_sql


def _resume_hard_delete_db():
    db = MagicMock()

    def execute(statement, _params=None):
        sql = str(statement)
        result = MagicMock()
        if sql.lstrip().startswith("SELECT id, images, deleted_at"):
            result.fetchall.return_value = [
                (9, '["images/resume/a.jpg"]', datetime(2026, 1, 1))
            ]
        elif sql.lstrip().startswith("DELETE FROM `resume`"):
            result.rowcount = 1
        else:
            raise AssertionError(f"unexpected SQL: {sql}")
        return result

    db.execute.side_effect = execute
    return db


@pytest.mark.parametrize(
    "media_marked,media_complete",
    [(1, True), (0, False)],
)
def test_resume_hard_delete_blocks_until_durable_media_finishes(
    monkeypatch, media_marked, media_complete,
):
    from app.services import job_media_service

    monkeypatch.setattr(
        job_media_service,
        "mark_resume_media_delete_pending",
        lambda *_: media_marked,
    )
    monkeypatch.setattr(
        job_media_service,
        "resume_hard_delete_media_complete",
        lambda *_: media_complete,
    )
    db = _resume_hard_delete_db()

    assert ttl_cleanup._hard_delete_expired_resumes(db, 7) == 0
    assert not any(
        str(call.args[0]).lstrip().startswith("DELETE FROM `resume`")
        for call in db.execute.call_args_list
    )


def test_resume_hard_delete_rechecks_media_state_in_delete(monkeypatch):
    from app.services import job_media_service

    monkeypatch.setattr(
        job_media_service,
        "mark_resume_media_delete_pending",
        lambda *_: 0,
    )
    monkeypatch.setattr(
        job_media_service,
        "resume_hard_delete_media_complete",
        lambda *_: True,
    )
    db = _resume_hard_delete_db()

    assert ttl_cleanup._hard_delete_expired_resumes(db, 7) == 1
    select_sql = str(db.execute.call_args_list[0].args[0])
    delete_sql = str(db.execute.call_args_list[1].args[0])
    assert "ORDER BY deleted_at, id" in select_sql
    assert "FOR UPDATE SKIP LOCKED" in select_sql
    assert "media_asset_lifecycle" in delete_sql
    assert "m.state<>'deleted'" in delete_sql
