"""Redis 集成测试（需要真实 Redis）。"""
import json
import time

import pytest
from redis.exceptions import ResponseError

from app.core.redis_client import (
    RedisDurabilityPolicyError,
    get_redis,
    get_session,
    save_session,
    save_session_if_version,
    delete_session,
    delete_session_if_version,
    check_msg_duplicate,
    check_rate_limit,
    enqueue_message,
    dequeue_message,
    user_lock,
    current_user_lock_fence,
    UserLockLost,
    QUEUE_INCOMING,
    RECOMMENDATION_SESSION_DELIVERY_INDEX_PREFIX,
    RECOMMENDATION_SESSION_INDEX_REGISTRY_PREFIX,
    RECOMMENDATION_SESSION_TARGET_INDEX_PREFIX,
    validate_redis_durability_policy,
)


def test_real_user_lock_lease_renews_beyond_original_ttl(monkeypatch):
    """A second owner must not enter even after the first lease's initial TTL elapsed."""
    from app.core import redis_client

    monkeypatch.setattr(redis_client, "LOCK_TTL", 2)
    monkeypatch.setattr(redis_client, "LOCK_RENEW_INTERVAL_SECONDS", 0.25)
    userid = "integration-lock-renewal"
    get_redis().delete(f"{redis_client.LOCK_PREFIX}{userid}")

    with redis_client.user_lock(userid, timeout=0) as first:
        assert first
        time.sleep(2.6)
        first.assert_owned()
        with redis_client.user_lock(userid, timeout=0) as second:
            assert not second

    with redis_client.user_lock(userid, timeout=0) as after_release:
        assert after_release


def test_real_session_cas_is_fenced_by_lock_owner():
    userid = "integration-fenced-session"
    delete_session(userid)
    try:
        with user_lock(userid, timeout=0) as lease:
            assert lease
            fence = current_user_lock_fence()
            assert fence is not None
            assert save_session_if_version(
                userid, {"role": "worker", "session_version": 1}, 0,
                lock_fence=fence,
            )

        with pytest.raises(UserLockLost):
            save_session_if_version(
                userid, {"role": "worker", "session_version": 2}, 1,
                lock_fence=fence,
            )
    finally:
        delete_session(userid)

pytestmark = pytest.mark.integration


class TestRedisConnection:
    def test_ping(self):
        r = get_redis()
        assert r.ping()

    def test_durability_policy_matches_fence_requirements(self):
        assert validate_redis_durability_policy() == {
            "maxmemory-policy": "noeviction",
            "appendonly": "yes",
            "appendfsync": "always",
        }

    @pytest.mark.parametrize(
        ("name", "unsafe_value", "safe_value"),
        [
            ("maxmemory-policy", "allkeys-lru", "noeviction"),
            ("appendfsync", "everysec", "always"),
        ],
    )
    def test_durability_policy_rejects_live_unsafe_config(
        self,
        name,
        unsafe_value,
        safe_value,
    ):
        r = get_redis()
        original = r.config_get(name)[name]
        assert original == safe_value
        try:
            r.config_set(name, unsafe_value)
            with pytest.raises(RedisDurabilityPolicyError, match=name):
                validate_redis_durability_policy(r)
        finally:
            r.config_set(name, original)

        assert validate_redis_durability_policy(r)[name] == safe_value


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

    def test_version_cas_rejects_stale_writer(self):
        userid = "test_user_cas_001"
        delete_session(userid)
        try:
            assert save_session_if_version(
                userid, {"role": "worker", "session_version": 1}, 0,
            ) is True
            assert save_session_if_version(
                userid, {"role": "worker", "session_version": 2}, 0,
            ) is False
            assert get_session(userid)["session_version"] == 1
        finally:
            delete_session(userid)

    def test_recommendation_reverse_indexes_follow_session_lifecycle(self):
        userid = "integration-recommendation-index"
        delivery_key = (
            f"{RECOMMENDATION_SESSION_DELIVERY_INDEX_PREFIX}delivery-1"
        )
        target_7 = (
            f"{RECOMMENDATION_SESSION_TARGET_INDEX_PREFIX}resume:7"
        )
        target_9 = (
            f"{RECOMMENDATION_SESSION_TARGET_INDEX_PREFIX}resume:9"
        )
        r = get_redis()
        delete_session(userid)
        r.delete(delivery_key, target_7, target_9)
        try:
            save_session(userid, {
                "role": "factory",
                "history": [{
                    "role": "assistant",
                    "content": "[recommendation_delivery]",
                    "delivery_id": "delivery-1",
                }],
                "candidate_snapshot": {
                    "direction": "search_worker",
                    "candidate_ids": ["7", "9"],
                },
            })
            assert r.smembers(delivery_key) == {userid}
            assert r.smembers(target_7) == {userid}
            assert r.smembers(target_9) == {userid}

            save_session(userid, {
                "role": "factory",
                "history": [],
                "candidate_snapshot": {
                    "direction": "search_worker",
                    "candidate_ids": ["9"],
                },
            })
            assert r.exists(delivery_key) == 0
            assert r.exists(target_7) == 0
            assert r.smembers(target_9) == {userid}
        finally:
            delete_session(userid)
            assert userid not in r.smembers(target_9)

    @pytest.mark.parametrize(
        ("operation", "wrong_role"),
        [
            ("save", "registry"),
            ("save", "existing_index"),
            ("save", "new_index"),
            ("cas_save", "registry"),
            ("cas_save", "existing_index"),
            ("cas_save", "new_index"),
            ("delete", "registry"),
            ("delete", "existing_index"),
            ("cas_delete", "registry"),
            ("cas_delete", "existing_index"),
        ],
    )
    def test_all_session_index_scripts_preflight_wrong_types(
        self,
        operation,
        wrong_role,
    ):
        userid = f"integration-wrong-type-{operation}-{wrong_role}"
        session_key = f"session:{userid}"
        registry_key = f"{RECOMMENDATION_SESSION_INDEX_REGISTRY_PREFIX}{userid}"
        existing_index = (
            f"{RECOMMENDATION_SESSION_DELIVERY_INDEX_PREFIX}old-{userid}"
        )
        candidate_id = f"new-{userid}"
        new_index = (
            f"{RECOMMENDATION_SESSION_TARGET_INDEX_PREFIX}resume:{candidate_id}"
        )
        keys = (session_key, registry_key, existing_index, new_index)
        r = get_redis()
        r.delete(*keys)
        original = json.dumps({"role": "old", "session_version": 1})
        r.set(session_key, original)
        if wrong_role == "registry":
            r.set(registry_key, "not-a-set")
        else:
            r.sadd(registry_key, existing_index)
            if wrong_role == "existing_index":
                r.set(existing_index, "not-a-set")
            else:
                r.sadd(existing_index, userid)
                r.set(new_index, "not-a-set")

        payload = {
            "role": "new",
            "session_version": 2,
            "candidate_snapshot": {
                "direction": "search_worker",
                "candidate_ids": [candidate_id],
            },
        }

        def _snapshot():
            snapshot = {}
            for key in keys:
                key_type = r.type(key)
                if key_type == "string":
                    content = r.get(key)
                elif key_type == "set":
                    content = sorted(r.smembers(key))
                else:
                    content = None
                snapshot[key] = (key_type, content, r.pttl(key))
            return snapshot

        before = _snapshot()
        try:
            with pytest.raises(
                ResponseError,
                match=f"SESSION_INDEX_WRONGTYPE {wrong_role}",
            ):
                if operation == "save":
                    save_session(userid, payload)
                elif operation == "cas_save":
                    save_session_if_version(userid, payload, 1)
                elif operation == "delete":
                    delete_session(userid)
                else:
                    delete_session_if_version(userid, 1)

            assert _snapshot() == before
        finally:
            r.delete(*keys)

    @pytest.mark.parametrize("operation", ["cas_save", "cas_delete"])
    def test_cas_scripts_do_not_write_after_malformed_session_json(
        self,
        operation,
    ):
        userid = f"integration-malformed-session-{operation}"
        session_key = f"session:{userid}"
        registry_key = f"{RECOMMENDATION_SESSION_INDEX_REGISTRY_PREFIX}{userid}"
        existing_index = (
            f"{RECOMMENDATION_SESSION_DELIVERY_INDEX_PREFIX}old-{userid}"
        )
        new_index = f"{RECOMMENDATION_SESSION_TARGET_INDEX_PREFIX}resume:{userid}"
        keys = (session_key, registry_key, existing_index, new_index)
        r = get_redis()
        r.delete(*keys)
        r.set(session_key, "{malformed-json")
        r.sadd(registry_key, existing_index)
        r.sadd(existing_index, userid)
        try:
            with pytest.raises(ResponseError):
                if operation == "cas_save":
                    save_session_if_version(userid, {
                        "session_version": 2,
                        "candidate_snapshot": {
                            "direction": "search_worker",
                            "candidate_ids": [userid],
                        },
                    }, 1)
                else:
                    delete_session_if_version(userid, 1)

            assert r.get(session_key) == "{malformed-json"
            assert r.smembers(registry_key) == {existing_index}
            assert r.smembers(existing_index) == {userid}
            assert r.exists(new_index) == 0
        finally:
            r.delete(*keys)

    def test_cas_session_indexes_preserve_state_on_stale_or_lost_fence(self):
        userid = "integration-cas-index-lifecycle"
        session_key = f"session:{userid}"
        registry_key = f"{RECOMMENDATION_SESSION_INDEX_REGISTRY_PREFIX}{userid}"
        old_index = f"{RECOMMENDATION_SESSION_DELIVERY_INDEX_PREFIX}cas-old"
        new_index = f"{RECOMMENDATION_SESSION_TARGET_INDEX_PREFIX}resume:cas-new"
        stale_index = (
            f"{RECOMMENDATION_SESSION_TARGET_INDEX_PREFIX}resume:cas-stale"
        )
        lock_key = "lock:integration-cas-index-lifecycle"
        keys = (
            session_key,
            registry_key,
            old_index,
            new_index,
            stale_index,
            lock_key,
        )
        r = get_redis()
        r.delete(*keys)
        try:
            save_session(userid, {
                "session_version": 1,
                "history": [{"delivery_id": "cas-old"}],
            })
            assert save_session_if_version(userid, {
                "session_version": 2,
                "candidate_snapshot": {
                    "direction": "search_worker",
                    "candidate_ids": ["cas-new"],
                },
            }, 1)
            assert r.exists(old_index) == 0
            assert r.smembers(registry_key) == {new_index}
            assert r.smembers(new_index) == {userid}
            committed = r.get(session_key)

            assert save_session_if_version(userid, {
                "session_version": 3,
                "candidate_snapshot": {
                    "direction": "search_worker",
                    "candidate_ids": ["cas-stale"],
                },
            }, 1) is False
            assert r.get(session_key) == committed
            assert r.smembers(registry_key) == {new_index}
            assert r.exists(stale_index) == 0

            r.set(lock_key, "new-owner")
            with pytest.raises(UserLockLost):
                save_session_if_version(
                    userid,
                    {"session_version": 3},
                    2,
                    lock_fence=(lock_key, "old-owner"),
                )
            assert delete_session_if_version(userid, 1) is False
            with pytest.raises(UserLockLost):
                delete_session_if_version(
                    userid,
                    2,
                    lock_fence=(lock_key, "old-owner"),
                )
            assert r.get(session_key) == committed
            assert r.smembers(registry_key) == {new_index}
            assert r.smembers(new_index) == {userid}

            assert delete_session_if_version(userid, 2) is True
            assert r.exists(session_key, registry_key, new_index) == 0
        finally:
            r.delete(*keys)


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
            assert bool(acquired) is True
            acquired.assert_owned()

    def test_lock_is_exclusive(self):
        """锁持有期间，同一 user 的第二次获取应超时失败。"""
        with user_lock("test_lock_002", timeout=5) as acquired_outer:
            assert bool(acquired_outer) is True
            # 内层尝试获取同一 user 的锁，timeout=1 秒后应失败
            with user_lock("test_lock_002", timeout=1) as acquired_inner:
                assert bool(acquired_inner) is False

    def test_different_users_independent(self):
        """不同 user 的锁互不干扰。"""
        with user_lock("test_lock_003a", timeout=5) as a:
            with user_lock("test_lock_003b", timeout=5) as b:
                assert bool(a) is True
                assert bool(b) is True
