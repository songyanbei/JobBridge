"""Redis 客户端封装。

职责：
- 会话状态管理（conversation_session）
- 分布式锁（消息串行化，见方案 §11.7）
- 配置缓存（system_config 热加载）
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
