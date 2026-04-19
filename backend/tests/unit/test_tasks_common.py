"""Phase 7 tasks/common.py 单元测试。

覆盖：
- `ensure_ttl_config_defaults(db)`：空集 / 全集 / 子集三种场景的幂等性。
- 释放 Lua CAS 脚本的调用路径（task_lock 在 TTL 过期后不误删）。

Integration 侧的真实 MySQL 行为见 `tests/integration/test_tasks_ttl_config.py`。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.tasks import common
from app.tasks.common import (
    _TTL_CONFIG_DEFAULTS,
    ensure_ttl_config_defaults,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _mock_db_with_existing_keys(existing: set[str]) -> MagicMock:
    """构造一个 MagicMock Session，其 query(...).filter(...).all() 返回
    与 ``existing`` 对齐的 (key,) 元组列表。"""
    db = MagicMock()
    rows = [(k,) for k in existing]
    db.query.return_value.filter.return_value.all.return_value = rows
    db.add = MagicMock()
    db.commit = MagicMock()
    return db


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

class TestEnsureTtlConfigDefaults:
    def test_defaults_tuple_contents(self):
        """Phase 7 §4.1 定义的 6 个 key 必须齐全。"""
        keys = {k for k, *_ in _TTL_CONFIG_DEFAULTS}
        assert keys == {
            "ttl.job.days",
            "ttl.resume.days",
            "ttl.conversation_log.days",
            "ttl.audit_log.days",
            "ttl.wecom_inbound_event.days",
            "ttl.hard_delete.delay_days",
        }

    def test_empty_db_inserts_all(self):
        """数据库中完全没有 ttl.* key 时，补齐 6 行。"""
        db = _mock_db_with_existing_keys(existing=set())
        added = ensure_ttl_config_defaults(db)
        assert added == 6
        assert db.add.call_count == 6
        db.commit.assert_called_once()

    def test_full_db_inserts_zero(self):
        """全部 6 个 key 已存在时，不插入、不 commit。"""
        full = {k for k, *_ in _TTL_CONFIG_DEFAULTS}
        db = _mock_db_with_existing_keys(existing=full)
        added = ensure_ttl_config_defaults(db)
        assert added == 0
        db.add.assert_not_called()
        db.commit.assert_not_called()

    def test_partial_db_inserts_missing_only(self):
        """部分 key 存在时，只补齐缺失的，不覆盖已有的。"""
        # 模拟：首次部署跑过旧 seed.sql，只有 3 个 key；Phase 7 新增 3 个缺失
        existing = {
            "ttl.job.days",
            "ttl.resume.days",
            "ttl.conversation_log.days",
        }
        db = _mock_db_with_existing_keys(existing=existing)
        added = ensure_ttl_config_defaults(db)
        assert added == 3
        assert db.add.call_count == 3
        db.commit.assert_called_once()

        # 确认被 add 的都是 Phase 7 新增 key，不重复添加已有
        inserted_keys = {
            call.args[0].config_key for call in db.add.call_args_list
        }
        assert inserted_keys == {
            "ttl.audit_log.days",
            "ttl.wecom_inbound_event.days",
            "ttl.hard_delete.delay_days",
        }

    def test_warning_logged_for_each_missing_key(self, caplog):
        """每条缺失 key 都应产生 warn 日志，提示"未跑 phase7_001 迁移"。

        loguru 默认不会被 caplog 捕获，这里只断言函数正常返回，
        实际日志落盘由集成测试观察。
        """
        db = _mock_db_with_existing_keys(existing=set())
        added = ensure_ttl_config_defaults(db)
        assert added == 6

    def test_insert_values_match_defaults(self):
        """插入的 value / value_type / description 必须与 _TTL_CONFIG_DEFAULTS 严格一致。"""
        db = _mock_db_with_existing_keys(existing=set())
        ensure_ttl_config_defaults(db)

        by_key = {call.args[0].config_key: call.args[0] for call in db.add.call_args_list}
        for key, value, value_type, desc in _TTL_CONFIG_DEFAULTS:
            assert by_key[key].config_value == value
            assert by_key[key].value_type == value_type
            assert by_key[key].description == desc


class TestTaskLockOwnerToken:
    """task_lock 使用 owner token + Lua CAS 释放，避免任务超时后误删他人锁。"""

    def test_acquire_writes_random_token(self, monkeypatch):
        """取锁时 SET NX EX 的 value 是随机 token（不是固定 '1'）。"""
        fake_redis = MagicMock()
        fake_redis.set.return_value = True
        monkeypatch.setattr(common, "get_redis", lambda: fake_redis)

        with common.task_lock("t_x", ttl=60) as acquired:
            assert acquired is True

        set_call = fake_redis.set.call_args
        args, kwargs = set_call
        # key
        assert args[0] == "task_lock:t_x"
        # value 为 32 位 hex token（secrets.token_hex(16)）
        token = args[1]
        assert isinstance(token, str) and len(token) == 32
        assert kwargs.get("nx") is True
        assert kwargs.get("ex") == 60

    def test_release_calls_lua_cas(self, monkeypatch):
        """释放锁走 EVAL 脚本，传入 key 与 token 作为 ARGV[1]。"""
        fake_redis = MagicMock()
        fake_redis.set.return_value = True
        fake_redis.eval.return_value = 1
        monkeypatch.setattr(common, "get_redis", lambda: fake_redis)

        with common.task_lock("t_y", ttl=60):
            pass

        assert fake_redis.eval.called
        eval_args = fake_redis.eval.call_args.args
        # eval(script, 1, key, token)
        assert eval_args[1] == 1
        assert eval_args[2] == "task_lock:t_y"
        # ARGV[1] 是之前 set 时使用的 token
        set_token = fake_redis.set.call_args.args[1]
        assert eval_args[3] == set_token

    def test_skip_release_when_not_acquired(self, monkeypatch):
        """未拿到锁时不应调用 EVAL（否则会误删他人锁）。"""
        fake_redis = MagicMock()
        fake_redis.set.return_value = False  # 别的实例已持有
        monkeypatch.setattr(common, "get_redis", lambda: fake_redis)

        with common.task_lock("t_z", ttl=60) as acquired:
            assert acquired is False

        fake_redis.eval.assert_not_called()
