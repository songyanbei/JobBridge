"""Phase 7 调度任务公共工具。

职责：
- `task_lock(name, ttl)`：Redis 分布式锁（owner token + Lua CAS 释放），
  保证 app 横向扩容时同一时刻只有一个实例执行任务，避免超时后误删他人锁。
- `log_event(event, **fields)`：loguru 结构化打点统一入口；所有运维事件走这里，
  不再扩展 `audit_log.action` 枚举（对齐 phase7-main.md §0 本版修订说明）。
"""
from __future__ import annotations

import secrets
from contextlib import contextmanager
from typing import Any, Generator

from loguru import logger

from app.core.redis_client import get_redis


# Lua 脚本：比较 value 与当前锁持有者一致时才删除，否则返回 0。
# 避免 TTL 过期后任务仍在执行、恰好另一实例获得了同名锁时被误删。
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


@contextmanager
def task_lock(name: str, ttl: int = 3600) -> Generator[bool, None, None]:
    """Redis 分布式任务锁。

    Args:
        name: 任务名（会组成 key 为 ``task_lock:{name}``）。
        ttl: 锁 TTL（秒），**必须 ≥ 任务预期最大耗时的 2 倍**。
             `ttl_cleanup` 建议 3600s，短任务 60~300s。

    Yields:
        bool: 是否成功持有锁。调用方应在未持有时静默跳过。

    Usage:
        with task_lock("ttl_cleanup", ttl=3600) as acquired:
            if not acquired:
                logger.info("ttl_cleanup: skipped, another instance holds the lock")
                return
            _run_ttl_cleanup()
    """
    r = get_redis()
    key = f"task_lock:{name}"
    token = secrets.token_hex(16)
    acquired = bool(r.set(key, token, nx=True, ex=ttl))
    try:
        yield acquired
    finally:
        if acquired:
            try:
                r.eval(_RELEASE_SCRIPT, 1, key, token)
            except Exception:
                logger.exception(f"task_lock release failed: name={name}")


def log_event(event: str, **fields: Any) -> None:
    """统一的 loguru 结构化事件打点。

    事件名用蛇形命名（如 `ttl_cleanup_summary` / `queue_backlog` / `llm_call`），
    字段走 kwargs 以 JSON 结构化落盘，不写 audit_log。
    """
    logger.bind(event=event, **fields).info(event)
