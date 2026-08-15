"""Redis 客户端封装。

职责：
- 会话状态管理（conversation_session）
- 分布式锁（消息串行化，见方案 §11.7）
- 配置缓存（system_config 热加载）
- Phase 5：审核工作台软锁、Undo 暂存、事件回传幂等、管理员登录失败计数
"""
import json
import hashlib
import logging
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Generator

import redis

from app.config import settings

logger = logging.getLogger(__name__)

# 全局 Redis 连接池（延迟初始化，首次使用时创建）
_pool: redis.ConnectionPool | None = None


def _get_pool() -> redis.ConnectionPool:
    global _pool
    if _pool is None:
        _pool = redis.ConnectionPool.from_url(
            settings.redis_url,
            decode_responses=True,
            max_connections=settings.redis_max_connections,
            socket_connect_timeout=settings.redis_connect_timeout_seconds,
            socket_timeout=settings.redis_socket_timeout_seconds,
            health_check_interval=30,
        )
    return _pool


def get_redis() -> redis.Redis:
    """获取 Redis 客户端实例。"""
    return redis.Redis(connection_pool=_get_pool())


REQUIRED_DURABILITY_POLICY = {
    "maxmemory-policy": "noeviction",
    "appendonly": "yes",
    "appendfsync": "always",
}


class RedisDurabilityPolicyError(RuntimeError):
    """Raised when Redis cannot safely hold revocation fences."""


class SessionCommitDeadlineExceeded(RuntimeError):
    """Raised before Redis writes when a durable commit deadline has passed."""


def validate_redis_durability_policy(
    client: redis.Redis | None = None,
) -> dict[str, str]:
    """Validate the Redis persistence and eviction settings used by fences."""
    redis_client = client or get_redis()
    actual: dict[str, str] = {}
    mismatches: list[str] = []
    for name, expected in REQUIRED_DURABILITY_POLICY.items():
        configured = redis_client.config_get(name).get(name)
        value = "" if configured is None else str(configured).lower()
        actual[name] = value
        if value != expected:
            mismatches.append(f"{name}={value or '<missing>'} (expected {expected})")

    if mismatches:
        raise RedisDurabilityPolicyError(
            "Redis durability policy is unsafe: " + "; ".join(mismatches)
        )
    return actual


# ---------------------------------------------------------------------------
# 会话状态操作
# ---------------------------------------------------------------------------

SESSION_PREFIX = "session:"
SESSION_TTL = 30 * 60  # 30 分钟
RECOMMENDATION_SESSION_DELIVERY_INDEX_PREFIX = (
    "recommendation:session:delivery:"
)
RECOMMENDATION_SESSION_TARGET_INDEX_PREFIX = "recommendation:session:target:"
RECOMMENDATION_SESSION_INDEX_REGISTRY_PREFIX = (
    "recommendation:session:indexes:"
)
RECOMMENDATION_SESSION_REVOCATION_FENCE_KEY = (
    "recommendation:session:revoked_indexes"
)


def recommendation_session_index_keys(session: dict) -> list[str]:
    """Derive bounded reverse-index keys from a redacted session payload."""
    keys: set[str] = set()
    history = session.get("history")
    if isinstance(history, list):
        for entry in history:
            if not isinstance(entry, dict):
                continue
            delivery_id = entry.get("delivery_id")
            if delivery_id:
                keys.add(
                    f"{RECOMMENDATION_SESSION_DELIVERY_INDEX_PREFIX}"
                    f"{delivery_id}"
                )

    snapshot = session.get("candidate_snapshot")
    if isinstance(snapshot, dict):
        direction = snapshot.get("direction")
        target_type = (
            "job" if direction == "search_job"
            else "resume" if direction == "search_worker"
            else None
        )
        candidate_ids = snapshot.get("candidate_ids")
        if target_type and isinstance(candidate_ids, list):
            for candidate_id in candidate_ids:
                if candidate_id in (None, ""):
                    continue
                keys.add(
                    f"{RECOMMENDATION_SESSION_TARGET_INDEX_PREFIX}"
                    f"{target_type}:{candidate_id}"
                )
    return sorted(keys)


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
    r.eval(
        _SAVE_SESSION_WITH_INDEXES_SCRIPT,
        3,
        f"{SESSION_PREFIX}{userid}",
        f"{RECOMMENDATION_SESSION_INDEX_REGISTRY_PREFIX}{userid}",
        RECOMMENDATION_SESSION_REVOCATION_FENCE_KEY,
        SESSION_TTL,
        json.dumps(session, ensure_ascii=False),
        userid,
        json.dumps(recommendation_session_index_keys(session)),
    )


_SAVE_SESSION_WITH_INDEXES_SCRIPT = """
local registry = KEYS[2]
local revoked_indexes = KEYS[3]
local userid = ARGV[3]
local ttl = tonumber(ARGV[1])
local new_indexes = cjson.decode(ARGV[4])
local function require_set_or_none(key, role)
    local key_type = redis.call('TYPE', key)['ok']
    if key_type ~= 'none' and key_type ~= 'set' then
        error(
            'SESSION_INDEX_WRONGTYPE ' .. role ..
            ' key=' .. key .. ' type=' .. key_type
        )
    end
    return key_type
end
local registry_type = require_set_or_none(registry, 'registry')
require_set_or_none(revoked_indexes, 'revocation_fence')
local old_indexes = {}
if registry_type == 'set' then
    old_indexes = redis.call('SMEMBERS', registry)
end
for _, index_key in ipairs(old_indexes) do
    require_set_or_none(index_key, 'existing_index')
end
for _, index_key in ipairs(new_indexes) do
    require_set_or_none(index_key, 'new_index')
    if redis.call('SISMEMBER', revoked_indexes, index_key) == 1 then
        error('SESSION_INDEX_REVOKED key=' .. index_key)
    end
end
redis.call('SETEX', KEYS[1], ARGV[1], ARGV[2])
for _, index_key in ipairs(old_indexes) do
    redis.call('SREM', index_key, userid)
    if redis.call('SCARD', index_key) == 0 then
        redis.call('DEL', index_key)
    end
end
redis.call('DEL', registry)
for _, index_key in ipairs(new_indexes) do
    redis.call('SADD', index_key, userid)
    redis.call('EXPIRE', index_key, ttl)
    redis.call('SADD', registry, index_key)
end
if #new_indexes > 0 then
    redis.call('EXPIRE', registry, ttl)
end
return 1
"""

_DELETE_SESSION_WITH_INDEXES_SCRIPT = """
local registry = KEYS[2]
local userid = ARGV[1]
local function require_set_or_none(key, role)
    local key_type = redis.call('TYPE', key)['ok']
    if key_type ~= 'none' and key_type ~= 'set' then
        error(
            'SESSION_INDEX_WRONGTYPE ' .. role ..
            ' key=' .. key .. ' type=' .. key_type
        )
    end
    return key_type
end
local registry_type = require_set_or_none(registry, 'registry')
local old_indexes = {}
if registry_type == 'set' then
    old_indexes = redis.call('SMEMBERS', registry)
end
for _, index_key in ipairs(old_indexes) do
    require_set_or_none(index_key, 'existing_index')
end
redis.call('DEL', KEYS[1])
for _, index_key in ipairs(old_indexes) do
    redis.call('SREM', index_key, userid)
    if redis.call('SCARD', index_key) == 0 then
        redis.call('DEL', index_key)
    end
end
redis.call('DEL', registry)
return 1
"""


_SAVE_SESSION_CAS_SCRIPT = """
if ARGV[4] ~= '' and redis.call('GET', KEYS[2]) ~= ARGV[4] then
    return -1
end
local current = redis.call('GET', KEYS[1])
local expected = tonumber(ARGV[1])
if current then
    local decoded = cjson.decode(current)
    local version = tonumber(decoded['session_version'] or 0)
    if version ~= expected then
        return 0
    end
elseif expected ~= 0 and ARGV[5] ~= '1' then
    return 0
end
local registry = KEYS[3]
local revoked_indexes = KEYS[4]
local userid = ARGV[6]
local new_indexes = cjson.decode(ARGV[7])
local function require_set_or_none(key, role)
    local key_type = redis.call('TYPE', key)['ok']
    if key_type ~= 'none' and key_type ~= 'set' then
        error(
            'SESSION_INDEX_WRONGTYPE ' .. role ..
            ' key=' .. key .. ' type=' .. key_type
        )
    end
    return key_type
end
local registry_type = require_set_or_none(registry, 'registry')
require_set_or_none(revoked_indexes, 'revocation_fence')
local old_indexes = {}
if registry_type == 'set' then
    old_indexes = redis.call('SMEMBERS', registry)
end
for _, index_key in ipairs(old_indexes) do
    require_set_or_none(index_key, 'existing_index')
end
for _, index_key in ipairs(new_indexes) do
    require_set_or_none(index_key, 'new_index')
    if redis.call('SISMEMBER', revoked_indexes, index_key) == 1 then
        error('SESSION_INDEX_REVOKED key=' .. index_key)
    end
end
if ARGV[8] ~= '' then
    local now = redis.call('TIME')
    local now_epoch = tonumber(now[1]) + tonumber(now[2]) / 1000000
    if now_epoch >= tonumber(ARGV[8]) then
        return -2
    end
end
redis.call('SETEX', KEYS[1], ARGV[2], ARGV[3])
for _, index_key in ipairs(old_indexes) do
    redis.call('SREM', index_key, userid)
    if redis.call('SCARD', index_key) == 0 then
        redis.call('DEL', index_key)
    end
end
redis.call('DEL', registry)
for _, index_key in ipairs(new_indexes) do
    redis.call('SADD', index_key, userid)
    redis.call('EXPIRE', index_key, ARGV[2])
    redis.call('SADD', registry, index_key)
end
if #new_indexes > 0 then
    redis.call('EXPIRE', registry, ARGV[2])
end
return 1
"""

_DELETE_SESSION_CAS_SCRIPT = """
if ARGV[2] ~= '' and redis.call('GET', KEYS[2]) ~= ARGV[2] then
    return -1
end
local current = redis.call('GET', KEYS[1])
local expected = tonumber(ARGV[1])
if not current then
    if expected ~= 0 then
        return 0
    end
else
    local decoded = cjson.decode(current)
    local version = tonumber(decoded['session_version'] or 0)
    if version ~= expected then
        return 0
    end
end
local registry = KEYS[3]
local userid = ARGV[3]
local function require_set_or_none(key, role)
    local key_type = redis.call('TYPE', key)['ok']
    if key_type ~= 'none' and key_type ~= 'set' then
        error(
            'SESSION_INDEX_WRONGTYPE ' .. role ..
            ' key=' .. key .. ' type=' .. key_type
        )
    end
    return key_type
end
local registry_type = require_set_or_none(registry, 'registry')
local old_indexes = {}
if registry_type == 'set' then
    old_indexes = redis.call('SMEMBERS', registry)
end
for _, index_key in ipairs(old_indexes) do
    require_set_or_none(index_key, 'existing_index')
end
if ARGV[4] ~= '' then
    local now = redis.call('TIME')
    local now_epoch = tonumber(now[1]) + tonumber(now[2]) / 1000000
    if now_epoch >= tonumber(ARGV[4]) then
        return -2
    end
end
redis.call('DEL', KEYS[1])
for _, index_key in ipairs(old_indexes) do
    redis.call('SREM', index_key, userid)
    if redis.call('SCARD', index_key) == 0 then
        redis.call('DEL', index_key)
    end
end
redis.call('DEL', registry)
return 1
"""


_FENCE_SESSION_INDEXES_SCRIPT = """
local fence = KEYS[1]
local function require_set_or_none(key, role)
    local key_type = redis.call('TYPE', key)['ok']
    if key_type ~= 'none' and key_type ~= 'set' then
        error(
            'SESSION_INDEX_WRONGTYPE ' .. role ..
            ' key=' .. key .. ' type=' .. key_type
        )
    end
end
require_set_or_none(fence, 'revocation_fence')
for _, index_key in ipairs(ARGV) do
    require_set_or_none(index_key, 'revoked_index')
end
for _, index_key in ipairs(ARGV) do
    redis.call('SADD', fence, index_key)
end
local members = {}
local seen = {}
for _, index_key in ipairs(ARGV) do
    for _, userid in ipairs(redis.call('SMEMBERS', index_key)) do
        if not seen[userid] then
            seen[userid] = true
            table.insert(members, userid)
        end
    end
end
return members
"""


_REMOVE_SESSION_INDEX_MEMBERS_SCRIPT = """
local registry = KEYS[1]
local userid = ARGV[1]
local registry_type = redis.call('TYPE', registry)['ok']
if registry_type ~= 'none' and registry_type ~= 'set' then
    error(
        'SESSION_INDEX_WRONGTYPE registry' ..
        ' key=' .. registry .. ' type=' .. registry_type
    )
end
for index = 2, #ARGV do
    local index_key = ARGV[index]
    local key_type = redis.call('TYPE', index_key)['ok']
    if key_type ~= 'none' and key_type ~= 'set' then
        error(
            'SESSION_INDEX_WRONGTYPE revoked_index' ..
            ' key=' .. index_key .. ' type=' .. key_type
        )
    end
end
for index = 2, #ARGV do
    redis.call('SREM', ARGV[index], userid)
    redis.call('SREM', registry, ARGV[index])
end
return 1
"""


def fence_recommendation_session_indexes(index_keys: list[str]) -> set[str]:
    """Fence revoked reverse indexes and return their current session owners."""
    if not index_keys:
        return set()
    members = get_redis().eval(
        _FENCE_SESSION_INDEXES_SCRIPT,
        1,
        RECOMMENDATION_SESSION_REVOCATION_FENCE_KEY,
        *sorted(set(index_keys)),
    )
    return {
        value.decode() if isinstance(value, bytes) else str(value)
        for value in members
    }


def remove_recommendation_session_index_members(
    userid: str,
    index_keys: list[str],
) -> None:
    """Remove one session from revoked indexes without deleting whole keys."""
    if not index_keys:
        return
    get_redis().eval(
        _REMOVE_SESSION_INDEX_MEMBERS_SCRIPT,
        1,
        f"{RECOMMENDATION_SESSION_INDEX_REGISTRY_PREFIX}{userid}",
        userid,
        *sorted(set(index_keys)),
    )


def save_session_if_version(
    userid: str,
    session: dict,
    expected_version: int,
    lock_fence: tuple[str, Any] | None = None,
    *,
    allow_missing: bool = False,
    deadline_epoch: Any = None,
) -> bool:
    """原子保存 session；仅当前版本等于 ``expected_version`` 时成功。

    旧 session 没有 ``session_version`` 时按 0 处理。该 CAS 是用户锁之外的
    fencing 防线：即使旧 Worker 因续租故障继续运行，也不能覆盖新 Worker 已提交
    的会话状态。
    """
    r = get_redis()
    lock_key, lock_token = lock_fence or ("__no_user_lock_fence__", "")
    result = r.eval(
        _SAVE_SESSION_CAS_SCRIPT,
        4,
        f"{SESSION_PREFIX}{userid}",
        lock_key,
        f"{RECOMMENDATION_SESSION_INDEX_REGISTRY_PREFIX}{userid}",
        RECOMMENDATION_SESSION_REVOCATION_FENCE_KEY,
        int(expected_version),
        SESSION_TTL,
        json.dumps(session, ensure_ascii=False),
        lock_token,
        "1" if allow_missing else "0",
        userid,
        json.dumps(recommendation_session_index_keys(session)),
        "" if deadline_epoch is None else str(deadline_epoch),
    )
    if int(result) == -1:
        raise UserLockLost("user lock fence rejected session commit")
    if int(result) == -2:
        raise SessionCommitDeadlineExceeded("durable session commit deadline exceeded")
    return bool(result)


def delete_session(userid: str) -> None:
    """清除用户会话状态。"""
    r = get_redis()
    r.eval(
        _DELETE_SESSION_WITH_INDEXES_SCRIPT,
        2,
        f"{SESSION_PREFIX}{userid}",
        f"{RECOMMENDATION_SESSION_INDEX_REGISTRY_PREFIX}{userid}",
        userid,
    )


def delete_session_if_version(
    userid: str,
    expected_version: int,
    lock_fence: tuple[str, Any] | None = None,
    *,
    deadline_epoch: Any = None,
) -> bool:
    """仅当 session 版本和用户锁 owner 都匹配时原子删除。"""
    r = get_redis()
    lock_key, lock_token = lock_fence or ("__no_user_lock_fence__", "")
    result = r.eval(
        _DELETE_SESSION_CAS_SCRIPT,
        3,
        f"{SESSION_PREFIX}{userid}",
        lock_key,
        f"{RECOMMENDATION_SESSION_INDEX_REGISTRY_PREFIX}{userid}",
        int(expected_version),
        lock_token,
        userid,
        "" if deadline_epoch is None else str(deadline_epoch),
    )
    if int(result) == -1:
        raise UserLockLost("user lock fence rejected session delete")
    if int(result) == -2:
        raise SessionCommitDeadlineExceeded("durable session commit deadline exceeded")
    return bool(result)


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
LOCK_RENEW_INTERVAL_SECONDS = 10


class UserLockLost(RuntimeError):
    """用户锁已丢失；当前处理结果不得继续提交。"""


class UserLockUnavailable(UserLockLost):
    """Redis unavailable while acquiring or verifying the user lock."""


@dataclass
class UserLockLease:
    acquired: bool
    lock: Any
    lost_event: threading.Event
    lock_id: str
    lock_key: str | None = None
    token: Any = None
    unavailable: bool = False

    def __bool__(self) -> bool:
        return self.acquired

    def assert_owned(self) -> None:
        """提交副作用前验证租约仍属于当前处理者。"""
        if self.unavailable:
            raise UserLockUnavailable(
                f"user lock service unavailable: lock_id={self.lock_id}",
            )
        if not self.acquired or self.lost_event.is_set():
            raise UserLockLost(f"user lock lease lost: lock_id={self.lock_id}")
        try:
            owned = self.lock.owned()
        except redis.exceptions.RedisError as exc:
            self.unavailable = True
            self.lost_event.set()
            raise UserLockUnavailable(
                f"unable to verify user lock: lock_id={self.lock_id}",
            ) from exc
        if not owned:
            self.lost_event.set()
            raise UserLockLost(f"user lock no longer owned: lock_id={self.lock_id}")

    def fence(self) -> tuple[str, Any]:
        self.assert_owned()
        if not self.lock_key or self.token in (None, "", b""):
            raise UserLockLost(f"user lock has no fence token: lock_id={self.lock_id}")
        return self.lock_key, self.token


_current_user_lock_lease: ContextVar[UserLockLease | None] = ContextVar(
    "current_user_lock_lease", default=None,
)


def current_user_lock_fence() -> tuple[str, Any] | None:
    lease = _current_user_lock_lease.get()
    return lease.fence() if lease is not None else None


def _renew_user_lock(
    lock,
    stop_event: threading.Event,
    lease: UserLockLease,
    lock_id: str,
) -> None:
    """在消息处理期间续租用户锁，避免慢 LLM 调用超过固定租约。

    ``thread_local=False`` 让续租线程和持有线程共享同一 lock token。续租失败
    后停止重试并记录错误；后续 P1 fencing/version CAS 会进一步阻止过期持有者
    提交，本函数先消除正常慢请求下必然过期的窗口。
    """
    while not stop_event.wait(LOCK_RENEW_INTERVAL_SECONDS):
        try:
            if not lock.extend(LOCK_TTL, replace_ttl=True):
                logger.error("user_lock renewal returned false: lock_id=%s", lock_id)
                lease.lost_event.set()
                return
        except redis.exceptions.LockError:
            lease.lost_event.set()
            logger.exception("user_lock renewal lost ownership: lock_id=%s", lock_id)
            return
        except redis.exceptions.RedisError:
            lease.unavailable = True
            lease.lost_event.set()
            logger.exception("user_lock renewal failed: lock_id=%s", lock_id)
            return


@contextmanager
def user_lock(userid: str, timeout: int = 10) -> Generator[UserLockLease, None, None]:
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
    lock_id = hashlib.sha256(lock_key.encode("utf-8")).hexdigest()[:12]
    # thread_local=False：redis-py 默认把 token 放在线程局部变量中；续租线程需要
    # 访问同一 token，否则 extend 会错误地认为当前线程不持有锁。
    lock = r.lock(
        lock_key,
        timeout=LOCK_TTL,
        blocking_timeout=timeout,
        thread_local=False,
    )
    try:
        acquired = lock.acquire(blocking=True)
    except redis.exceptions.RedisError:
        # Redis 短断时不能让异常穿透并终止 Worker 主循环。按“未获得锁”返回，
        # Worker 会尝试重入队；若重入队同样失败，则把 durable event 标成
        # processing 交给启动恢复。
        logger.exception("user_lock acquire failed: lock_id=%s", lock_id)
        yield UserLockLease(
            False,
            lock,
            threading.Event(),
            lock_id,
            lock_key,
            unavailable=True,
        )
        return
    stop_renewal = threading.Event()
    lost_event = threading.Event()
    token = getattr(getattr(lock, "local", None), "token", None)
    lease = UserLockLease(acquired, lock, lost_event, lock_id, lock_key, token)
    renewal_thread: threading.Thread | None = None
    if acquired:
        renewal_thread = threading.Thread(
            target=_renew_user_lock,
            args=(lock, stop_renewal, lease, lock_id),
            name="user-lock-renewal",
            daemon=True,
        )
        renewal_thread.start()
    context_token = _current_user_lock_lease.set(lease) if acquired else None
    try:
        yield lease
    finally:
        if context_token is not None:
            _current_user_lock_lease.reset(context_token)
        if acquired:
            stop_renewal.set()
            if renewal_thread is not None:
                renewal_thread.join(timeout=1)
            try:
                lock.release()
            except redis.exceptions.LockNotOwnedError:
                logger.error("user_lock lost before release: lock_id=%s", lock_id)
            except redis.exceptions.RedisError:
                # 业务提交前已有 lease.assert_owned fencing；释放阶段网络失败只会
                # 让锁等 TTL 自动过期，不能反向把已完成消息变成 Worker 崩溃。
                logger.exception("user_lock release failed: lock_id=%s", lock_id)


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


def get_undo(target_type: str, target_id: int | str) -> dict | None:
    """读取 Undo 快照但不消费，用于执行前的生命周期门禁。"""
    data = get_redis().get(f"{UNDO_PREFIX}{target_type}:{target_id}")
    if not data:
        return None
    try:
        return json.loads(data)
    except Exception:
        return None


def get_undo_snapshot(
    target_type: str, target_id: int | str,
) -> tuple[dict, str] | None:
    """Return the parsed Undo payload together with its exact Redis value."""
    data = get_redis().get(f"{UNDO_PREFIX}{target_type}:{target_id}")
    if not data:
        return None
    try:
        return json.loads(data), str(data)
    except Exception:
        return None


_CONSUME_UNDO_IF_UNCHANGED_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if not current then
    return 0
end
if current ~= ARGV[1] then
    return -1
end
redis.call('DEL', KEYS[1])
return 1
"""

_VALIDATE_UNDO_UNCHANGED_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if not current then
    return 0
end
if current ~= ARGV[1] then
    return -1
end
return 1
"""


def validate_undo_unchanged(
    target_type: str, target_id: int | str, expected_value: str,
) -> str:
    """Validate the exact snapshot without consuming it before DB commit."""
    key = f"{UNDO_PREFIX}{target_type}:{target_id}"
    result = int(get_redis().eval(
        _VALIDATE_UNDO_UNCHANGED_SCRIPT,
        1,
        key,
        expected_value,
    ))
    return {1: "unchanged", 0: "missing", -1: "changed"}.get(result, "changed")


def consume_undo_if_unchanged(
    target_type: str, target_id: int | str, expected_value: str,
) -> str:
    """Atomically consume only the exact Undo snapshot already validated."""
    key = f"{UNDO_PREFIX}{target_type}:{target_id}"
    result = int(get_redis().eval(
        _CONSUME_UNDO_IF_UNCHANGED_SCRIPT,
        1,
        key,
        expected_value,
    ))
    return {1: "consumed", 0: "missing", -1: "changed"}.get(result, "changed")


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


# ---------------------------------------------------------------------------
# 推荐 v1：动态总开关分发通道（方案 §7.5 / §11.8）
# ---------------------------------------------------------------------------

# DB 才是真源；Redis 只是提交之后的加速通道，因此 key 带 30 秒 TTL：Pub/Sub 丢包
# 由订阅方的 5 秒 DB 轮询收敛，key 过期后读取方自然回源，不会长期钉住旧值。
RUNTIME_CONTROL_KEY = "recommendation:runtime_control"
RUNTIME_CONTROL_CHANNEL = "recommendation:runtime_control"
RUNTIME_CONTROL_TTL_SECONDS = 30


def publish_runtime_control(payload: dict) -> bool:
    """write-through 写 key 并广播 Pub/Sub；失败返回 False 交给调用方降级。

    payload 必须携带 ``revision``，订阅方只接受不小于本地 revision 的更新。
    """
    body = json.dumps(payload, ensure_ascii=False, default=str)
    try:
        r = get_redis()
        pipe = r.pipeline()
        pipe.setex(RUNTIME_CONTROL_KEY, RUNTIME_CONTROL_TTL_SECONDS, body)
        pipe.publish(RUNTIME_CONTROL_CHANNEL, body)
        pipe.execute()
        return True
    except Exception:
        logger.warning("runtime control write-through failed", exc_info=True)
        return False


def read_runtime_control() -> dict | None:
    """读取 Redis 侧总开关快照；key 缺失或 Redis 故障统一返回 None。"""
    try:
        raw = get_redis().get(RUNTIME_CONTROL_KEY)
    except Exception:
        logger.warning("runtime control read failed", exc_info=True)
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def runtime_control_pubsub():
    """返回已订阅总开关频道的 pubsub 对象；Redis 不可用时返回 None。"""
    try:
        pubsub = get_redis().pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(RUNTIME_CONTROL_CHANNEL)
        return pubsub
    except Exception:
        logger.warning("runtime control subscribe failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# 推荐 v1：不可变策略版本缓存（方案 §11.8）
# ---------------------------------------------------------------------------

# 只缓存"已发布/已归档"这类禁止再修改的 version 行，key 带 version ID，因此缓存
# 回填永远不会把新指针覆盖成旧指针。可变的 release 指针明确不缓存。
STRATEGY_VERSION_CACHE_PREFIX = "recommendation:strategy:"
STRATEGY_VERSION_CACHE_TTL = 600


def get_cached_strategy_version(version_id: int | str) -> dict | None:
    try:
        raw = get_redis().get(f"{STRATEGY_VERSION_CACHE_PREFIX}{version_id}")
    except Exception:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def set_cached_strategy_version(
    version_id: int | str,
    payload: dict,
    ttl: int = STRATEGY_VERSION_CACHE_TTL,
) -> None:
    try:
        get_redis().setex(
            f"{STRATEGY_VERSION_CACHE_PREFIX}{version_id}",
            ttl,
            json.dumps(payload, ensure_ascii=False, default=str),
        )
    except Exception:
        pass
