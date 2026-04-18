"""每日 03:00 TTL 清理与硬删除任务（Phase 7 §3.1 模块 B）。

处理顺序（按 phase7-dev-implementation.md §5.4）：
1. 岗位过期软删：expires_at < NOW() 且 delist_reason IS NULL → delist_reason='expired' + deleted_at=NOW()
2. 简历过期软删：expires_at < NOW() 且 deleted_at IS NULL → deleted_at=NOW()
3. 岗位 7 天硬删（分批）
4. 简历 7 天硬删 + storage.delete() 附件
5. 用户主动删除 7 天硬删：其 resume / conversation_log 残留硬删，user 记录保留
6. conversation_log > 30 天硬删
7. wecom_inbound_event > 30 天硬删
8. audit_log > 180 天硬删

约束：
- 分批（LIMIT 500 per batch）+ 每批独立 commit，避免锁表。
- 每步 try/except 独立，单步失败不影响其它步骤。
- 所有汇总写 loguru 结构化日志，不写 audit_log（对齐 §0 本版修订）。
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


def _hard_delete_expired_jobs(db) -> int:
    """岗位软删 7 天后硬删。"""
    return _batch_hard_delete(db, "job", "deleted_at IS NOT NULL AND deleted_at < NOW() - INTERVAL 7 DAY")


def _hard_delete_expired_resumes(db) -> int:
    """简历软删 7 天后硬删；删除前先收集 images 中的对象存储 key 并调用 storage.delete()。"""
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
                "WHERE deleted_at IS NOT NULL AND deleted_at < NOW() - INTERVAL 7 DAY "
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


def _hard_delete_deleted_users(db) -> int:
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
    # 先找出目标 userid 列表
    candidate_rows = db.execute(
        text(
            """
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
                ) < UTC_TIMESTAMP() - INTERVAL 7 DAY
                -- 兜底：无 extra.deleted_at 时用 audit_log；NOW() 与 created_at
                -- 在同一 server time zone 下比较，避开 CONVERT_TZ(SYSTEM) 返回 NULL
                OR (
                  JSON_EXTRACT(user.extra, '$.deleted_at') IS NULL
                  AND al.last_delete_at IS NOT NULL
                  AND al.last_delete_at < NOW() - INTERVAL 7 DAY
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
            _safe_step("soft_delete_jobs", stats, lambda: _soft_delete_expired_jobs(db))
            _safe_step("soft_delete_resumes", stats, lambda: _soft_delete_expired_resumes(db))
            _safe_step("hard_delete_jobs", stats, lambda: _hard_delete_expired_jobs(db))
            _safe_step("hard_delete_resumes", stats, lambda: _hard_delete_expired_resumes(db))
            _safe_step("hard_delete_deleted_users", stats, lambda: _hard_delete_deleted_users(db))
            _safe_step(
                "hard_delete_conversation",
                stats,
                lambda: _batch_hard_delete(
                    db, "conversation_log", "created_at < NOW() - INTERVAL 30 DAY"
                ),
            )
            _safe_step(
                "hard_delete_inbound",
                stats,
                lambda: _batch_hard_delete(
                    db, "wecom_inbound_event", "created_at < NOW() - INTERVAL 30 DAY"
                ),
            )
            _safe_step(
                "hard_delete_audit_log",
                stats,
                lambda: _batch_hard_delete(
                    db, "audit_log", "created_at < NOW() - INTERVAL 180 DAY"
                ),
            )

        log_event("ttl_cleanup_summary", **stats)
