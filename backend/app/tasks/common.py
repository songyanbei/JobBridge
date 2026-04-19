"""Phase 7 调度任务公共工具。

职责：
- `task_lock(name, ttl)`：Redis 分布式锁（owner token + Lua CAS 释放），
  保证 app 横向扩容时同一时刻只有一个实例执行任务，避免超时后误删他人锁。
- `log_event(event, **fields)`：loguru 结构化打点统一入口；所有运维事件走这里，
  不再扩展 `audit_log.action` 枚举（对齐 phase7-main.md §0 本版修订说明）。
- `ensure_ttl_config_defaults(db)`：旧库启动自愈。`seed.sql` 只在首次初始化执行，
  既有 volume 的 MySQL 不会自动补齐 Phase 7 新增的 `ttl.*` key；本函数在
  scheduler.start() 与 ttl_cleanup.run() 两处调用，幂等补齐缺失 key
  并 warn 提示运维（对齐 phase7-main.md §4.1 / §0.1 U2）。
"""
from __future__ import annotations

import secrets
from contextlib import contextmanager
from typing import Any, Generator

from loguru import logger
from sqlalchemy.orm import Session

from app.core.redis_client import get_redis
from app.models import SystemConfig


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


# ---------------------------------------------------------------------------
# TTL config 自愈（Phase 7 §0.1 U2 / §4.1）
# ---------------------------------------------------------------------------

# (config_key, default_value, value_type, description)
_TTL_CONFIG_DEFAULTS: tuple[tuple[str, str, str, str], ...] = (
    ("ttl.job.days",                 "30",  "int", "岗位 TTL（天）"),
    ("ttl.resume.days",              "30",  "int", "简历 TTL（天）"),
    ("ttl.conversation_log.days",    "30",  "int", "对话日志 TTL（天）"),
    ("ttl.audit_log.days",           "180", "int", "审核日志 TTL（天）— Phase 7 新增"),
    ("ttl.wecom_inbound_event.days", "30",  "int", "入站事件表 TTL（天）— Phase 7 新增"),
    ("ttl.hard_delete.delay_days",   "7",   "int", "软删到硬删延迟（天）— Phase 7 新增"),
)


def ensure_ttl_config_defaults(db: Session) -> int:
    """幂等补齐 TTL 相关 system_config key；返回新插入条数。

    Context:
        ``backend/sql/seed.sql`` 只在 Docker MySQL 首次初始化时由
        ``/docker-entrypoint-initdb.d`` 执行；**已上线环境**的 volume 不会重跑，
        因此 Phase 7 新增的 ``ttl.audit_log.days`` / ``ttl.wecom_inbound_event.days``
        / ``ttl.hard_delete.delay_days`` 缺失时，运营在后台修改会"感觉改了但不生效"。

    Mitigation:
        正式路径：执行 ``backend/sql/migrations/phase7_001_ensure_system_config.sql``。
        本函数作为应用层自愈兜底，每次发现并补齐时都会打 ``logger.warning``，
        方便运维定位"升级时漏跑迁移"的环境。

    Idempotent:
        只 INSERT 缺失 key，不覆盖已有 key；0 插入时完全无副作用。

    Args:
        db: 调用方提供的 SQLAlchemy Session，函数内部会 commit 新增行。

    Returns:
        int: 本次新补齐的 key 数（0 = 数据库已齐全，正常情况）。
    """
    keys = [k for k, *_ in _TTL_CONFIG_DEFAULTS]
    existing = {
        row[0]
        for row in db.query(SystemConfig.config_key)
        .filter(SystemConfig.config_key.in_(keys))
        .all()
    }
    added = 0
    for key, value, value_type, desc in _TTL_CONFIG_DEFAULTS:
        if key in existing:
            continue
        db.add(SystemConfig(
            config_key=key,
            config_value=value,
            value_type=value_type,
            description=desc,
        ))
        added += 1
        logger.warning(
            "ensure_ttl_config_defaults: inserted missing key {key!r}={value!r}. "
            "Existing environment did NOT run phase7_001 migration; "
            "run backend/sql/migrations/phase7_001_ensure_system_config.sql "
            "to make upgrade path explicit.",
            key=key, value=value,
        )
    if added:
        db.commit()
    return added
