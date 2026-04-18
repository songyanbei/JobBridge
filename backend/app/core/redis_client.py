"""Redis 客户端封装。

职责：
- 会话状态管理（conversation_session）
- 分布式锁（消息串行化，见方案 §11.7）
- 配置缓存（system_config 热加载）
- Phase 5：审核工作台软锁、Undo 暂存、事件回传幂等、管理员登录失败计数
"""
import json
from contextlib import contextmanager
from typing import Any, Generator

import redis

from app.config import settings

# 全局 Redis 连接池（延迟初始化，首次使用时创建）
_pool: redis.ConnectionPool | None = None


def _get_pool() -> redis.ConnectionPool:
    global _pool
    if _pool is None:
        _pool = redis.ConnectionPool.from_url(
            settings.redis_url,
            decode_responses=True,
            max_connections=settings.redis_max_connections,
        )
    return _pool


def get_redis() -> redis.Redis:
    """获取 Redis 客户端实例。"""
    return redis.Redis(connection_pool=_get_pool())


# ---------------------------------------------------------------------------
# 会话状态操作
# ---------------------------------------------------------------------------

SESSION_PREFIX = "session:"
SESSION_TTL = 30 * 60  # 30 分钟


def get_session(userid: str) -> dict | None:
    """读取用户会话状态。"""
    r = get_redis()
    data = r.get(f"{SESSION_PREFIX}{userid}")
    if data is None:
        return None
    return json.loads(data)


def save_session(userid: str, session: dict) -> None:
    """保存用户会话状态（自动续 TTL）。"""
    r = get_redis()
    r.setex(f"{SESSION_PREFIX}{userid}", SESSION_TTL, json.dumps(session, ensure_ascii=False))


def delete_session(userid: str) -> None:
    """清除用户会话状态。"""
    r = get_redis()
    r.delete(f"{SESSION_PREFIX}{userid}")


# ---------------------------------------------------------------------------
# 分布式锁（消息串行化 §11.7）
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 消息入队（队列操作）
# ---------------------------------------------------------------------------

QUEUE_INCOMING = "queue:incoming"
QUEUE_DEAD_LETTER = "queue:dead_letter"
QUEUE_SEND_RETRY = "queue:send_retry"
QUEUE_RATE_LIMIT_NOTIFY = "queue:rate_limit_notify"
# Phase 7：群消息推送失败重试队列（独立于 QUEUE_SEND_RETRY：后者 payload 形态为
# {userid, content, ...}，消费侧调用 send_text；群消息 payload 含 chat_id，不兼容）。
QUEUE_GROUP_SEND_RETRY = "queue:group_send_retry"


def enqueue_message(message_json: str, queue: str = QUEUE_INCOMING) -> None:
    """将消息推入指定队列。

    Args:
        message_json: 消息 JSON 字符串
        queue: 目标队列 key，默认为 queue:incoming，
               死信场景传入 QUEUE_DEAD_LETTER
    """
    r = get_redis()
    r.rpush(queue, message_json)


def dequeue_message(timeout: int = 0) -> str | None:
    """从待处理队列阻塞取出消息。"""
    r = get_redis()
    result = r.blpop(QUEUE_INCOMING, timeout=timeout)
    if result is None:
        return None
    return result[1]


# ---------------------------------------------------------------------------
# 幂等检查
# ---------------------------------------------------------------------------

MSG_DEDUP_PREFIX = "msg:"
MSG_DEDUP_TTL = 600  # 10 分钟


def check_msg_duplicate(msg_id: str) -> bool:
    """检查消息是否重复。返回 True 表示重复（应忽略）。"""
    r = get_redis()
    return not r.set(f"{MSG_DEDUP_PREFIX}{msg_id}", "1", ex=MSG_DEDUP_TTL, nx=True)


# ---------------------------------------------------------------------------
# 限流（防刷 §12.5）
# ---------------------------------------------------------------------------

RATE_LIMIT_PREFIX = "rate:"


def check_rate_limit(userid: str, window: int = 10, max_count: int = 5) -> bool:
    """用户级消息限流。返回 True 表示允许通过，False 表示被限流。"""
    r = get_redis()
    key = f"{RATE_LIMIT_PREFIX}{userid}"
    current = r.incr(key)
    if current == 1:
        r.expire(key, window)
    return current <= max_count


# ---------------------------------------------------------------------------
# 分布式锁（消息串行化 §11.7）
# ---------------------------------------------------------------------------

LOCK_PREFIX = "lock:"
LOCK_TTL = 30  # 30 秒，防止死锁


@contextmanager
def user_lock(userid: str, timeout: int = 10) -> Generator[bool, None, None]:
    """Per-user 分布式锁，保证同一用户消息串行处理。

    Usage:
        with user_lock(userid) as acquired:
            if acquired:
                # 处理消息
            else:
                # 获取锁超时，返回"请稍候"
    """
    r = get_redis()
    lock_key = f"{LOCK_PREFIX}{userid}"
    lock = r.lock(lock_key, timeout=LOCK_TTL, blocking_timeout=timeout)
    acquired = lock.acquire(blocking=True)
    try:
        yield acquired
    finally:
        if acquired:
            try:
                lock.release()
            except redis.exceptions.LockNotOwnedError:
                pass  # TTL 已过期自动释放


# ---------------------------------------------------------------------------
# Phase 5：审核工作台软锁（§5.2）
# ---------------------------------------------------------------------------

AUDIT_LOCK_PREFIX = "audit_lock:"
AUDIT_LOCK_TTL = 300  # 5 分钟


def acquire_audit_lock(target_type: str, target_id: int | str, operator: str, ttl: int = AUDIT_LOCK_TTL) -> bool:
    """尝试获取审核软锁，返回 True 表示成功持有。"""
    r = get_redis()
    key = f"{AUDIT_LOCK_PREFIX}{target_type}:{target_id}"
    return bool(r.set(key, operator, nx=True, ex=ttl))


def refresh_audit_lock(target_type: str, target_id: int | str, operator: str, ttl: int = AUDIT_LOCK_TTL) -> bool:
    """如果当前锁由 operator 持有则续期。"""
    r = get_redis()
    key = f"{AUDIT_LOCK_PREFIX}{target_type}:{target_id}"
    holder = r.get(key)
    if holder == operator:
        r.expire(key, ttl)
        return True
    return False


def get_audit_lock_holder(target_type: str, target_id: int | str) -> str | None:
    """返回当前锁持有者 username，如未锁定返回 None。"""
    r = get_redis()
    return r.get(f"{AUDIT_LOCK_PREFIX}{target_type}:{target_id}")


def release_audit_lock(target_type: str, target_id: int | str, operator: str) -> bool:
    """仅持有者可释放锁，返回是否成功释放。"""
    r = get_redis()
    key = f"{AUDIT_LOCK_PREFIX}{target_type}:{target_id}"
    if r.get(key) == operator:
        r.delete(key)
        return True
    return False


# ---------------------------------------------------------------------------
# Phase 5：Undo 动作暂存（§5.2）
# ---------------------------------------------------------------------------

UNDO_PREFIX = "undo_action:"
UNDO_TTL = 30  # 30 秒


def save_undo(target_type: str, target_id: int | str, payload: dict, ttl: int = UNDO_TTL) -> None:
    r = get_redis()
    key = f"{UNDO_PREFIX}{target_type}:{target_id}"
    r.setex(key, ttl, json.dumps(payload, ensure_ascii=False, default=str))


def pop_undo(target_type: str, target_id: int | str) -> dict | None:
    """取出并删除 Undo 快照；超过 TTL 返回 None。"""
    r = get_redis()
    key = f"{UNDO_PREFIX}{target_type}:{target_id}"
    data = r.get(key)
    if not data:
        return None
    r.delete(key)
    try:
        return json.loads(data)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Phase 5：事件回传幂等（§5.9）
# ---------------------------------------------------------------------------

EVENT_IDEM_PREFIX = "event_idem:"
EVENT_DEDUPE_TTL_DEFAULT = 600  # 10 分钟


def mark_event_idem(userid: str, target_type: str, target_id: int | str, ttl: int = EVENT_DEDUPE_TTL_DEFAULT) -> bool:
    """标记事件幂等 key；返回 True 表示首次出现（需写库），False 表示已去重。"""
    r = get_redis()
    key = f"{EVENT_IDEM_PREFIX}{userid}:{target_type}:{target_id}"
    return bool(r.set(key, "1", nx=True, ex=ttl))


def clear_event_idem(userid: str, target_type: str, target_id: int | str) -> None:
    """写库失败时调用，让下次同事件可以重试。"""
    r = get_redis()
    r.delete(f"{EVENT_IDEM_PREFIX}{userid}:{target_type}:{target_id}")


# ---------------------------------------------------------------------------
# Phase 5：管理员登录失败计数（§5.1）
# ---------------------------------------------------------------------------

ADMIN_LOGIN_FAIL_PREFIX = "admin_login_fail:"
ADMIN_LOGIN_FAIL_TTL = 60


def incr_admin_login_fail(username: str) -> int:
    r = get_redis()
    key = f"{ADMIN_LOGIN_FAIL_PREFIX}{username}"
    count = r.incr(key)
    if count == 1:
        r.expire(key, ADMIN_LOGIN_FAIL_TTL)
    return count


def get_admin_login_fail(username: str) -> int:
    r = get_redis()
    v = r.get(f"{ADMIN_LOGIN_FAIL_PREFIX}{username}")
    return int(v) if v else 0


def clear_admin_login_fail(username: str) -> None:
    r = get_redis()
    r.delete(f"{ADMIN_LOGIN_FAIL_PREFIX}{username}")


# ---------------------------------------------------------------------------
# Phase 5：system_config 缓存
# ---------------------------------------------------------------------------

CONFIG_CACHE_PREFIX = "config_cache:"
CONFIG_CACHE_TTL = 300


def invalidate_config_cache(key: str | None = None) -> None:
    """清除单项或全量 config_cache。"""
    r = get_redis()
    if key:
        r.delete(f"{CONFIG_CACHE_PREFIX}{key}")
    r.delete(f"{CONFIG_CACHE_PREFIX}all")


def get_cached_config(key: str) -> str | None:
    """读取 Redis 端的配置缓存；失败返回 None 由调用方回源 DB。"""
    try:
        r = get_redis()
        return r.get(f"{CONFIG_CACHE_PREFIX}{key}")
    except Exception:
        return None


def set_cached_config(key: str, value: str, ttl: int = CONFIG_CACHE_TTL) -> None:
    """回填配置缓存；静默忽略 Redis 异常。"""
    try:
        r = get_redis()
        r.setex(f"{CONFIG_CACHE_PREFIX}{key}", ttl, value)
    except Exception:
        pass
