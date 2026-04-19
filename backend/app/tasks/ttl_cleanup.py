"""每日 03:00 TTL 清理与硬删除任务（Phase 7 §3.1 模块 B）。

处理顺序（按 phase7-dev-implementation.md §5.4）：
1. 岗位过期软删：expires_at < NOW() 且 delist_reason IS NULL → delist_reason='expired' + deleted_at=NOW()
2. 简历过期软删：expires_at < NOW() 且 deleted_at IS NULL → deleted_at=NOW()
3. 岗位软删后 ``ttl.hard_delete.delay_days`` 天硬删（分批）
4. 简历软删后 ``ttl.hard_delete.delay_days`` 天硬删 + storage.delete() 附件
5. 用户主动删除后 ``ttl.hard_delete.delay_days`` 天硬删其 resume / conversation_log 残留，user 保留
6. conversation_log > ``ttl.conversation_log.days`` 天硬删
7. wecom_inbound_event > ``ttl.wecom_inbound_event.days`` 天硬删
8. audit_log > ``ttl.audit_log.days`` 天硬删

约束：
- 分批（LIMIT 500 per batch）+ 每批独立 commit，避免锁表。
- 每步 try/except 独立，单步失败不影响其它步骤。
- 所有汇总写 loguru 结构化日志，不写 audit_log（对齐 §0 本版修订）。
- 天数一律从 ``system_config`` 运行时读取（phase7-main.md §4.1 新增 / 确认的 6 个 TTL key）。
  读取失败兜底默认值 7/30/30/180。
"""
from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import text

from app.db import SessionLocal
from app.storage import get_storage
from app.tasks.common import log_event, task_lock

BATCH_SIZE = 500


# ---------------------------------------------------------------------------
# system_config 读取
# ---------------------------------------------------------------------------

def _read_int_config(db, key: str, default: int) -> int:
    """从 ``system_config`` 读取整型配置，解析失败兜底 ``default``。"""
    row = db.execute(
        text("SELECT config_value FROM system_config WHERE config_key = :k"),
        {"k": key},
    ).first()
    if row is None or row[0] is None:
        return default
    try:
        return int(row[0])
    except (TypeError, ValueError):
        logger.warning(
            "ttl_cleanup: invalid int for %s=%r, using default %d", key, row[0], default
        )
        return default


def _load_ttl_config(db) -> dict[str, int]:
    """一次性加载本次清理所需的全部天数配置。

    字段含义（phase7-main.md §4.1）：
    - ``hard_delete_delay_days`` = ``ttl.hard_delete.delay_days`` （软删→硬删间隔，默认 7）
    - ``conversation_log_days`` = ``ttl.conversation_log.days`` （默认 30）
    - ``wecom_inbound_event_days`` = ``ttl.wecom_inbound_event.days`` （默认 30）
    - ``audit_log_days`` = ``ttl.audit_log.days`` （默认 180）

    说明：``ttl.job.days`` / ``ttl.resume.days`` 在 upload_service 写 expires_at
    时消费，本任务不再二次读取。
    """
    return {
        "hard_delete_delay_days": _read_int_config(db, "ttl.hard_delete.delay_days", 7),
        "conversation_log_days": _read_int_config(db, "ttl.conversation_log.days", 30),
        "wecom_inbound_event_days": _read_int_config(db, "ttl.wecom_inbound_event.days", 30),
        "audit_log_days": _read_int_config(db, "ttl.audit_log.days", 180),
    }


# ---------------------------------------------------------------------------
# 公共工具
# ---------------------------------------------------------------------------

def _batch_hard_delete(db, table: str, where: str) -> int:
    """按 WHERE 条件分批硬删指定表。返回总删除行数。

    每批独立 commit；若本批少于 BATCH_SIZE 说明已全部清理完成。
    """
    total = 0
    while True:
        result = db.execute(
            text(f"DELETE FROM `{table}` WHERE {where} LIMIT {BATCH_SIZE}")
        )
        db.commit()
        deleted = int(result.rowcount or 0)
        total += deleted
        if deleted < BATCH_SIZE:
            break
    return total


def _safe_step(step_name: str, stats: dict, fn) -> None:
    """把每一步包在 try/except 内，单步失败不影响其它步骤。"""
    try:
        stats[step_name] = fn()
    except Exception:
        logger.exception(f"ttl_cleanup step failed: {step_name}")
        stats[step_name] = -1  # -1 表示失败


# ---------------------------------------------------------------------------
# 各步骤实现
# ---------------------------------------------------------------------------

def _soft_delete_expired_jobs(db) -> int:
    """岗位过期软删：标记 delist_reason='expired' 并写 deleted_at。"""
    result = db.execute(
        text(
            "UPDATE `job` SET delist_reason='expired', deleted_at=NOW() "
            "WHERE expires_at < NOW() AND delist_reason IS NULL AND deleted_at IS NULL"
        )
    )
    db.commit()
    return int(result.rowcount or 0)


def _soft_delete_expired_resumes(db) -> int:
    """简历过期软删：写 deleted_at。"""
    result = db.execute(
        text(
            "UPDATE `resume` SET deleted_at=NOW() "
            "WHERE expires_at < NOW() AND deleted_at IS NULL"
        )
    )
    db.commit()
    return int(result.rowcount or 0)


def _hard_delete_expired_jobs(db, delay_days: int) -> int:
    """岗位软删 ``delay_days`` 天后硬删。"""
    return _batch_hard_delete(
        db, "job",
        f"deleted_at IS NOT NULL AND deleted_at < NOW() - INTERVAL {int(delay_days)} DAY",
    )


def _hard_delete_expired_resumes(db, delay_days: int) -> int:
    """简历软删 ``delay_days`` 天后硬删；删除前先收集 images 中的对象存储 key 并调用 storage.delete()。"""
    storage = None
    try:
        storage = get_storage()
    except Exception:
        logger.exception("ttl_cleanup: get_storage failed (skip storage cleanup)")

    total_deleted = 0
    while True:
        rows = db.execute(
            text(
                "SELECT id, images FROM `resume` "
                f"WHERE deleted_at IS NOT NULL AND deleted_at < NOW() - INTERVAL {int(delay_days)} DAY "
                f"LIMIT {BATCH_SIZE}"
            )
        ).fetchall()
        if not rows:
            break

        # 1) 清理对象存储
        for rid, images in rows:
            keys = _extract_image_keys(images)
            for key in keys:
                if storage is None:
                    continue
                try:
                    storage.delete(key)
                except Exception:
                    logger.exception(
                        f"ttl_cleanup: storage.delete failed key={key} resume_id={rid}"
                    )

        # 2) 删库
        ids = [str(r[0]) for r in rows]
        id_list = ",".join(ids)
        result = db.execute(text(f"DELETE FROM `resume` WHERE id IN ({id_list})"))
        db.commit()
        deleted = int(result.rowcount or 0)
        total_deleted += deleted
        if deleted < BATCH_SIZE:
            break
    return total_deleted


def _extract_image_keys(images: Any) -> list[str]:
    """images 列存的是 storage key 数组，既可能是 list 也可能是 JSON 字符串。"""
    if images is None:
        return []
    if isinstance(images, list):
        return [str(k) for k in images if k]
    if isinstance(images, (bytes, str)):
        import json
        try:
            data = json.loads(images)
        except Exception:
            return []
        if isinstance(data, list):
            return [str(k) for k in data if k]
    return []


def _hard_delete_deleted_users(db, delay_days: int) -> int:
    """用户主动 /删除我的信息 7 天后，硬删其 resume / conversation_log 残留，user 记录保留。

    7 天计时起点（对齐 phase7-main.md §3.1 模块 C）：
    - 主：User.extra['deleted_at']（UTC 字符串 `YYYY-MM-DD HH:MM:SS`）→ STR_TO_DATE 解析后
      与 UTC_TIMESTAMP() 比较。
    - 兜底：最新一条 AuditLog(target_type='user', action='auto_pass',
            operator='system', reason LIKE '%/删除我的信息%') 的 created_at
            与 NOW() 比较（二者遵循同一个 server time zone，无需 CONVERT_TZ 也正确）。

    实现上在一条 SQL 里用 OR 表达两条并行的比较，避开 MySQL
    ``CONVERT_TZ(x, @@session.time_zone, '+00:00')`` 在 ``time_zone='SYSTEM'`` 时
    返回 NULL 的坑。

    约束：user 记录不删除（防止重复注册），仅硬删其 resume / conversation_log 残留；
    所有 resume 的 storage 清理由前一步 ``_hard_delete_expired_resumes`` 负责
    （``delete_user_data()`` 已在执行时把 resume.deleted_at 设为 now）。
    """
    # 先找出目标 userid 列表；delay_days 已由调用方校验为 int
    safe_delay = int(delay_days)
    candidate_rows = db.execute(
        text(
            f"""
            SELECT user.external_userid
            FROM `user`
            LEFT JOIN (
              SELECT target_id, MAX(created_at) AS last_delete_at
              FROM `audit_log`
              WHERE target_type='user' AND action='auto_pass'
                AND operator='system' AND reason LIKE '%/删除我的信息%'
              GROUP BY target_id
            ) al ON al.target_id = user.external_userid
            WHERE user.status='deleted'
              AND (
                -- 主：extra.deleted_at 是 UTC 字符串，直接与 UTC_TIMESTAMP() 比
                STR_TO_DATE(
                  JSON_UNQUOTE(JSON_EXTRACT(user.extra, '$.deleted_at')),
                  '%Y-%m-%d %H:%i:%s'
                ) < UTC_TIMESTAMP() - INTERVAL {safe_delay} DAY
                -- 兜底：无 extra.deleted_at 时用 audit_log；NOW() 与 created_at
                -- 在同一 server time zone 下比较，避开 CONVERT_TZ(SYSTEM) 返回 NULL
                OR (
                  JSON_EXTRACT(user.extra, '$.deleted_at') IS NULL
                  AND al.last_delete_at IS NOT NULL
                  AND al.last_delete_at < NOW() - INTERVAL {safe_delay} DAY
                )
              )
            """
        )
    ).fetchall()

    userids = [row[0] for row in candidate_rows]
    if not userids:
        return 0

    total_deleted = 0
    for uid in userids:
        # 硬删该用户 resume（含 storage 清理）
        try:
            total_deleted += _batch_hard_delete(
                db,
                "resume",
                f"owner_userid = {_escape_literal(uid)}",
            )
        except Exception:
            logger.exception(f"ttl_cleanup: hard delete resume failed userid={uid}")
        # 硬删其 conversation_log 残留
        try:
            total_deleted += _batch_hard_delete(
                db,
                "conversation_log",
                f"userid = {_escape_literal(uid)}",
            )
        except Exception:
            logger.exception(f"ttl_cleanup: hard delete conversation_log failed userid={uid}")

    log_event("ttl_cleanup_deleted_users", userid_count=len(userids), rows_deleted=total_deleted)
    return total_deleted


def _escape_literal(value: str) -> str:
    """最小 SQL 字面量转义（只接受 external_userid 合法字符）。

    external_userid 是企微分配的 ASCII 串，实际不会包含单引号；
    此处做一次简单转义仅为防御性编码。
    """
    safe = value.replace("\\", "\\\\").replace("'", "''")
    return f"'{safe}'"


# ---------------------------------------------------------------------------
# 任务入口
# ---------------------------------------------------------------------------

def run() -> None:
    """APScheduler 调起的任务入口。"""
    with task_lock("ttl_cleanup", ttl=3600) as acquired:
        if not acquired:
            logger.info("ttl_cleanup: skipped, another instance holds the lock")
            return

        stats: dict = {}
        with SessionLocal() as db:
            # Phase 7：天数全量从 system_config 读取，失败兜底默认值
            cfg = _load_ttl_config(db)
            stats["_config"] = cfg  # 记录本次使用的配置，便于事后复盘
            delay = cfg["hard_delete_delay_days"]
            conv_days = cfg["conversation_log_days"]
            inbound_days = cfg["wecom_inbound_event_days"]
            audit_days = cfg["audit_log_days"]

            _safe_step("soft_delete_jobs", stats, lambda: _soft_delete_expired_jobs(db))
            _safe_step("soft_delete_resumes", stats, lambda: _soft_delete_expired_resumes(db))
            _safe_step("hard_delete_jobs", stats, lambda: _hard_delete_expired_jobs(db, delay))
            _safe_step(
                "hard_delete_resumes",
                stats,
                lambda: _hard_delete_expired_resumes(db, delay),
            )
            _safe_step(
                "hard_delete_deleted_users",
                stats,
                lambda: _hard_delete_deleted_users(db, delay),
            )
            _safe_step(
                "hard_delete_conversation",
                stats,
                lambda: _batch_hard_delete(
                    db, "conversation_log",
                    f"created_at < NOW() - INTERVAL {int(conv_days)} DAY",
                ),
            )
            _safe_step(
                "hard_delete_inbound",
                stats,
                lambda: _batch_hard_delete(
                    db, "wecom_inbound_event",
                    f"created_at < NOW() - INTERVAL {int(inbound_days)} DAY",
                ),
            )
            _safe_step(
                "hard_delete_audit_log",
                stats,
                lambda: _batch_hard_delete(
                    db, "audit_log",
                    f"created_at < NOW() - INTERVAL {int(audit_days)} DAY",
                ),
            )

        log_event("ttl_cleanup_summary", **stats)
