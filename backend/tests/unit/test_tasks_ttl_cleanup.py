"""Phase 7 tasks/ttl_cleanup.py 单元测试。

验证：
- ``_load_ttl_config`` 在 DB 缺失/有值/非法值三种场景下的回退行为
- ``_safe_step`` 捕获异常并写 -1 而不影响其它步骤
- ``_extract_image_keys`` 兼容 list / JSON 字符串 / None / 非法 JSON
- ``_batch_hard_delete`` 分批 DELETE LIMIT 500，最后一批 < 500 时退出
- ``_escape_literal`` 防御性转义单引号 / 反斜杠
- ``run()`` 未获取锁时直接 return，不读 DB
"""
from __future__ import annotations

import json
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
# _extract_image_keys
# ---------------------------------------------------------------------------

class TestExtractImageKeys:
    def test_none_returns_empty(self):
        assert ttl_cleanup._extract_image_keys(None) == []

    def test_list_passthrough(self):
        assert ttl_cleanup._extract_image_keys(["a/b.jpg", "c.png"]) == ["a/b.jpg", "c.png"]

    def test_list_filters_falsy(self):
        assert ttl_cleanup._extract_image_keys(["a", "", None, "b"]) == ["a", "b"]

    def test_json_string(self):
        raw = json.dumps(["x.jpg", "y.png"])
        assert ttl_cleanup._extract_image_keys(raw) == ["x.jpg", "y.png"]

    def test_json_bytes(self):
        raw = json.dumps(["x.jpg"]).encode("utf-8")
        assert ttl_cleanup._extract_image_keys(raw) == ["x.jpg"]

    def test_invalid_json_returns_empty(self):
        assert ttl_cleanup._extract_image_keys("{not json") == []

    def test_json_non_list_returns_empty(self):
        assert ttl_cleanup._extract_image_keys(json.dumps({"k": "v"})) == []


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
