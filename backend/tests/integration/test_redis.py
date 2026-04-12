"""Redis 集成测试（需要真实 Redis）。"""
import json

import pytest

from app.core.redis_client import (
    get_redis,
    get_session,
    save_session,
    delete_session,
    check_msg_duplicate,
    check_rate_limit,
    enqueue_message,
    dequeue_message,
    user_lock,
    QUEUE_INCOMING,
)

pytestmark = pytest.mark.integration


class TestRedisConnection:
    def test_ping(self):
        r = get_redis()
        assert r.ping()


class TestSessionOperations:
    def test_save_and_get(self):
        save_session("test_user_001", {"role": "worker", "intent": "search_job"})
        data = get_session("test_user_001")
        assert data is not None
        assert data["role"] == "worker"
        # 清理
        delete_session("test_user_001")

    def test_get_nonexistent(self):
        assert get_session("nonexistent_user_xyz") is None

    def test_delete(self):
        save_session("test_user_002", {"role": "factory"})
        delete_session("test_user_002")
        assert get_session("test_user_002") is None


class TestDedup:
    def test_first_time_not_duplicate(self):
        r = get_redis()
        key = "msg:test_dedup_001"
        r.delete(key)  # 清理
        assert check_msg_duplicate("test_dedup_001") is False

    def test_second_time_is_duplicate(self):
        r = get_redis()
        key = "msg:test_dedup_002"
        r.delete(key)  # 清理
        check_msg_duplicate("test_dedup_002")
        assert check_msg_duplicate("test_dedup_002") is True


class TestRateLimit:
    def test_within_limit(self):
        r = get_redis()
        r.delete("rate:test_rate_001")
        assert check_rate_limit("test_rate_001", window=10, max_count=5) is True

    def test_exceeds_limit(self):
        r = get_redis()
        r.delete("rate:test_rate_002")
        for _ in range(5):
            check_rate_limit("test_rate_002", window=10, max_count=5)
        assert check_rate_limit("test_rate_002", window=10, max_count=5) is False


class TestQueue:
    """消息队列 enqueue / dequeue 测试。"""

    def _cleanup(self):
        r = get_redis()
        r.delete(QUEUE_INCOMING)

    def test_enqueue_and_dequeue(self):
        self._cleanup()
        msg = json.dumps({"msg_id": "q_001", "text": "hello"})
        enqueue_message(msg)
        result = dequeue_message(timeout=1)
        assert result is not None
        parsed = json.loads(result)
        assert parsed["msg_id"] == "q_001"
        self._cleanup()

    def test_dequeue_empty_returns_none(self):
        self._cleanup()
        result = dequeue_message(timeout=1)
        assert result is None

    def test_fifo_order(self):
        """队列保持 FIFO 顺序。"""
        self._cleanup()
        enqueue_message("first")
        enqueue_message("second")
        enqueue_message("third")
        assert dequeue_message(timeout=1) == "first"
        assert dequeue_message(timeout=1) == "second"
        assert dequeue_message(timeout=1) == "third"
        self._cleanup()


class TestUserLock:
    """分布式锁（per-user 串行化）测试。"""

    def test_acquire_and_release(self):
        """正常获取和释放锁。"""
        with user_lock("test_lock_001", timeout=5) as acquired:
            assert acquired is True

    def test_lock_is_exclusive(self):
        """锁持有期间，同一 user 的第二次获取应超时失败。"""
        with user_lock("test_lock_002", timeout=5) as acquired_outer:
            assert acquired_outer is True
            # 内层尝试获取同一 user 的锁，timeout=1 秒后应失败
            with user_lock("test_lock_002", timeout=1) as acquired_inner:
                assert acquired_inner is False

    def test_different_users_independent(self):
        """不同 user 的锁互不干扰。"""
        with user_lock("test_lock_003a", timeout=5) as a:
            with user_lock("test_lock_003b", timeout=5) as b:
                assert a is True
                assert b is True
