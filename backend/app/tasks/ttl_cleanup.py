"""每日 03:00 TTL 清理与硬删除任务（Phase 7 §3.1 模块 B）。

处理顺序（按 phase7-dev-implementation.md §5.4）：
0. 推荐正文/session patch 到期清理与推荐明细 90 天硬删（§9.11；删除顺序被外键强制）
1. 岗位过期软删：expires_at < NOW() 且 delist_reason IS NULL → delist_reason='expired' + deleted_at=NOW()
2. 简历过期软删：expires_at < NOW() 且 deleted_at IS NULL → deleted_at=NOW()
3. 岗位软删后 ``ttl.hard_delete.delay_days`` 天硬删（分批）
4. 简历软删后 ``ttl.hard_delete.delay_days`` 天硬删 + storage.delete() 附件
5. 用户主动删除后 ``ttl.hard_delete.delay_days`` 天硬删其 resume /
   conversation_log / outbox / 终态 inbound 残留，user 保留
6. conversation_log > ``ttl.conversation_log.days`` 天硬删
7. 已发送 outbox > ``ttl.wecom_inbound_event.days`` 天硬删；出站死信按 audit TTL 保留
8. wecom_inbound_event > ``ttl.wecom_inbound_event.days`` 天硬删
9. audit_log > ``ttl.audit_log.days`` 天硬删

约束：
- 分批（LIMIT 500 per batch）+ 每批独立 commit，避免锁表。
- 每步 try/except 独立，单步失败不影响其它步骤。
- 所有汇总写 loguru 结构化日志，不写 audit_log（对齐 §0 本版修订）。
- 天数一律从 ``system_config`` 运行时读取（phase7-main.md §4.1 新增 / 确认的 6 个 TTL key）。
  读取失败兜底默认值 7/30/30/180。
"""
from __future__ import annotations

from loguru import logger
from sqlalchemy import text

from app.core.logging_setup import identifier_hash
from app.db import SessionLocal
from app.tasks.common import ensure_ttl_config_defaults, log_event, task_lock

BATCH_SIZE = 500


def _redact_expired_recommendation_content(db) -> int:
    """清理到期的推荐正文与 prepared session patch（§9.11 行 2108-2110）。

    状态机口径严格对齐 §9.6 行 1921 的
    ``prepared/pending/sending/retry_wait/sent/permanent_failed/unknown``：

    - ``sent`` 是终态，TTL 只清空 ``content_ciphertext``，**不得改写 status**
      （旧实现写成 ``redacted``，会让曝光派生和指标口径全部错位）；
    - 还没发出去就失去正文的 prepared/pending/retry_wait 转 ``permanent_failed``
      （旧实现写的 ``expired`` 不在枚举内）；
    - sending 且租约已过期转 ``unknown``，不自动重发（§10.3）。
    """
    unknown = db.execute(text(
        "UPDATE recommendation_delivery d "
        "JOIN wecom_outbound_outbox o ON o.recommendation_delivery_id=d.delivery_id "
        "SET d.status='unknown', d.last_error='sending lease expired after content TTL', "
        "d.last_error_code='sending_lease_expired', "
        "o.status='dead_letter', o.locked_at=NULL, o.next_attempt_at=NULL, "
        "o.last_error='ambiguous provider outcome; automatic resend disabled' "
        "WHERE d.status='sending' AND d.lease_expires_at IS NOT NULL "
        "AND d.lease_expires_at <= NOW(6) "
        "AND d.content_expires_at IS NOT NULL AND d.content_expires_at <= NOW(6)"
    ))
    expired = db.execute(text(
        "UPDATE recommendation_delivery d "
        "LEFT JOIN wecom_outbound_outbox o ON o.recommendation_delivery_id=d.delivery_id "
        "SET d.content_ciphertext=NULL, d.session_patch_ciphertext=NULL, "
        # MySQL 的多列 UPDATE 是左到右求值，后面的 CASE 会读到已经被改写的列值，
        # 因此所有依赖旧 status 的赋值必须排在 status 之前。
        "d.last_error=CASE WHEN d.status IN ('prepared','pending','retry_wait') "
        "             THEN 'recommendation content expired before send' ELSE d.last_error END, "
        # P2-12：retry_wait 同样是“还没发出去”的可恢复状态，必须纳入。
        "d.status=CASE WHEN d.status IN ('prepared','pending','retry_wait') "
        "             THEN 'permanent_failed' ELSE d.status END, "
        "o.last_error=CASE WHEN o.status='pending' "
        "                 THEN 'recommendation delivery expired before send' ELSE o.last_error END, "
        "o.status=CASE WHEN o.status='pending' THEN 'dead_letter' ELSE o.status END, "
        "o.locked_at=NULL, o.next_attempt_at=NULL "
        "WHERE d.status IN ('prepared','pending','retry_wait','sent','permanent_failed','unknown') "
        "AND d.content_expires_at IS NOT NULL AND d.content_expires_at <= NOW(6)"
    ))
    # §9.11 行 2109：prepared 超过 24 小时仍无法提交 session 时转 permanent_failed
    # 并同时清空正文，避免可解密的推荐正文无限期停留。
    stale_prepared = db.execute(text(
        "UPDATE recommendation_delivery "
        "SET status='permanent_failed', content_ciphertext=NULL, "
        "session_patch_ciphertext=NULL, "
        "last_error='prepared session never committed within 24h', "
        "last_error_code='session_commit_timeout' "
        "WHERE status='prepared' AND created_at < NOW(6) - INTERVAL 24 HOUR"
    ))
    # unknown 最多保留 7 天正文，供人工按 msgid 核对后再清理。
    stale_unknown = db.execute(text(
        "UPDATE recommendation_delivery "
        "SET content_ciphertext=NULL, session_patch_ciphertext=NULL "
        "WHERE status='unknown' AND content_ciphertext IS NOT NULL "
        "AND updated_at < NOW(6) - INTERVAL 7 DAY"
    ))
    db.commit()
    return (
        int(unknown.rowcount or 0)
        + int(expired.rowcount or 0)
        + int(stale_prepared.rowcount or 0)
        + int(stale_unknown.rowcount or 0)
    )


# ---------------------------------------------------------------------------
# 推荐明细 90 天 TTL（§9.11 行 2106-2107 / 2133-2143）
# ---------------------------------------------------------------------------

RECOMMENDATION_DETAIL_RETENTION_DAYS = 90


def _purge_expired_recommendation_details(db, retention_days: int) -> int:
    """按 §9.11 行 2133-2143 的固定顺序分批删除过期推荐明细。

    顺序是被真实外键强制的（RESTRICT/CASCADE/SET NULL 已按 §9.11 建好），错一步
    直接被数据库挡住：

    ``归因 event 脱钩 → impression → delivery → request.served_attempt_id 置 NULL
    → attempt → request``

    每批 ``BATCH_SIZE`` 条 request 独立 commit，避免长事务锁住在线推荐写入
    （§9.11 行 2117）。
    """
    days = int(retention_days)
    total = 0
    while True:
        request_rows = db.execute(
            text(
                "SELECT request_id FROM recommendation_request "
                f"WHERE created_at < NOW(6) - INTERVAL {days} DAY "
                f"LIMIT {BATCH_SIZE}"
            )
        ).fetchall()
        request_ids = [row[0] for row in request_rows]
        if not request_ids:
            break

        id_list = ",".join(_escape_literal(rid) for rid in request_ids)
        # 1) 归因 event 脱钩：event_log.delivery_id 虽然是 ON DELETE SET NULL，
        #    但 request_id/snapshot_id 没有外键，必须显式清掉，否则留下悬空引用。
        db.execute(text(
            "UPDATE `event_log` SET delivery_id=NULL, request_id=NULL, snapshot_id=NULL "
            "WHERE request_id IN (" + id_list + ") "
            "OR delivery_id IN (SELECT delivery_id FROM recommendation_delivery "
            "WHERE request_id IN (" + id_list + "))"
        ))
        # 2) impression（impression.request_id 是 RESTRICT）
        deleted = db.execute(text(
            "DELETE FROM recommendation_impression WHERE request_id IN (" + id_list + ")"
        ))
        total += int(deleted.rowcount or 0)
        # 3) delivery（delivery.request_id 是 RESTRICT）
        deleted = db.execute(text(
            "DELETE FROM recommendation_delivery WHERE request_id IN (" + id_list + ")"
        ))
        total += int(deleted.rowcount or 0)
        # 4) request.served_attempt_id 置 NULL，才能删 attempt
        db.execute(text(
            "UPDATE recommendation_request SET served_attempt_id=NULL "
            "WHERE served_attempt_id IN (SELECT attempt_id FROM recommendation_search_attempt "
            "WHERE request_id IN (" + id_list + "))"
        ))
        # 5) attempt（attempt.request_id 是 RESTRICT）
        deleted = db.execute(text(
            "DELETE FROM recommendation_search_attempt WHERE request_id IN (" + id_list + ")"
        ))
        total += int(deleted.rowcount or 0)
        # 6) request（parent_request_id 自引用是 SET NULL，子请求不会被挡住）
        deleted = db.execute(text(
            "DELETE FROM recommendation_request WHERE request_id IN (" + id_list + ")"
        ))
        total += int(deleted.rowcount or 0)
        db.commit()

        if len(request_ids) < BATCH_SIZE:
            break
    return total


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
    """每日兜底复用高频任务的行锁、状态复核和 durable 清理生产逻辑。"""
    from app.config import settings

    if not settings.job_expiry_cleanup_enabled:
        log_event("job_expiry_cleanup_disabled", source="daily_fallback")
        return 0
    from app.tasks.job_expiry_cleanup import process_expired_jobs
    stats = process_expired_jobs(db, max_runtime_seconds=None)
    return int(stats["processed"])


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
    """Fail-closed Job hard delete with replacement, cleanup and media gates."""
    from app.config import settings
    from app.models import JobReplacement
    from app.services.job_media_service import (
        hard_delete_media_complete,
        mark_job_media_delete_pending,
    )
    from app.services.target_cleanup_service import job_cleanup_succeeded

    if not settings.job_hard_delete_enabled:
        log_event("job_hard_delete_disabled")
        return 0

    deleted = 0
    cursor_deleted_at = None
    cursor_id = 0
    while True:
        cursor_sql = ""
        params = {"cursor_deleted_at": cursor_deleted_at, "cursor_id": cursor_id}
        if cursor_deleted_at is not None:
            cursor_sql = (
                "AND (deleted_at > :cursor_deleted_at "
                "OR (deleted_at = :cursor_deleted_at AND id > :cursor_id)) "
            )
        rows = db.execute(text(
            "SELECT id, images, deleted_at FROM `job` "
            f"WHERE deleted_at IS NOT NULL AND deleted_at < NOW() - INTERVAL {int(delay_days)} DAY "
            + cursor_sql
            + f"ORDER BY deleted_at, id LIMIT {BATCH_SIZE}"
        ), params).fetchall()
        if not rows:
            break
        for job_id, images, deleted_at in rows:
            job_id = int(job_id)
            active_relation = db.query(JobReplacement.id).filter(
                (
                    (JobReplacement.old_job_id == job_id)
                    | (JobReplacement.new_job_id == job_id)
                ),
                JobReplacement.lifecycle_status.in_(("awaiting_review", "conflict")),
            ).first()
            if active_relation is not None:
                continue
            if mark_job_media_delete_pending(db, job_id):
                continue
            if not job_cleanup_succeeded(db, int(job_id)):
                continue
            if not hard_delete_media_complete(db, int(job_id), images):
                continue
            result = db.execute(text(
                "DELETE FROM `job` WHERE id=:job_id "
                f"AND deleted_at < NOW() - INTERVAL {int(delay_days)} DAY "
                "AND EXISTS (SELECT 1 FROM `target_cleanup_task` t "
                "WHERE t.target_type='job' AND t.target_id=:job_id "
                "AND t.status='succeeded') "
                "AND NOT EXISTS (SELECT 1 FROM `media_asset_lifecycle` m "
                "WHERE m.entity_type='job' AND m.entity_id=:job_id "
                "AND m.state<>'deleted') "
                "AND NOT EXISTS (SELECT 1 FROM `job_replacement` r "
                "WHERE (r.old_job_id=:job_id OR r.new_job_id=:job_id) "
                "AND r.lifecycle_status IN ('awaiting_review','conflict'))"
            ), {"job_id": job_id})
            deleted += int(result.rowcount or 0)
        db.commit()
        cursor_id = int(rows[-1][0])
        cursor_deleted_at = rows[-1][2]
        if len(rows) < BATCH_SIZE:
            break
    return deleted


def _hard_delete_expired_resumes(db, delay_days: int) -> int:
    """Hard-delete resumes only after durable media cleanup is complete."""
    from app.services.job_media_service import (
        mark_resume_media_delete_pending,
        resume_hard_delete_media_complete,
    )

    total_deleted = 0
    cursor_deleted_at = None
    cursor_id = 0
    while True:
        cursor_sql = ""
        params = {"cursor_deleted_at": cursor_deleted_at, "cursor_id": cursor_id}
        if cursor_deleted_at is not None:
            cursor_sql = (
                "AND (deleted_at > :cursor_deleted_at "
                "OR (deleted_at = :cursor_deleted_at AND id > :cursor_id)) "
            )
        rows = db.execute(text(
            "SELECT id, images, deleted_at FROM `resume` "
            f"WHERE deleted_at IS NOT NULL AND deleted_at < NOW() - INTERVAL {int(delay_days)} DAY "
            + cursor_sql
            + f"ORDER BY deleted_at, id LIMIT {BATCH_SIZE} FOR UPDATE SKIP LOCKED"
        ), params).fetchall()
        if not rows:
            break

        # Hand attached objects to the durable worker before checking the gate.
        for resume_id, images, _deleted_at in rows:
            resume_id = int(resume_id)
            if mark_resume_media_delete_pending(db, resume_id):
                continue
            if not resume_hard_delete_media_complete(db, resume_id, images):
                continue
            result = db.execute(text(
                "DELETE FROM `resume` WHERE id=:resume_id "
                f"AND deleted_at < NOW() - INTERVAL {int(delay_days)} DAY "
                "AND NOT EXISTS (SELECT 1 FROM `media_asset_lifecycle` m "
                "WHERE m.entity_type='resume' AND m.entity_id=:resume_id "
                "AND m.state<>'deleted')"
            ), {"resume_id": resume_id})
            total_deleted += int(result.rowcount or 0)

        db.commit()
        cursor_id = int(rows[-1][0])
        cursor_deleted_at = rows[-1][2]
        if len(rows) < BATCH_SIZE:
            break
    return total_deleted


def _hard_delete_deleted_users(db, delay_days: int) -> int:
    """用户主动 /删除我的信息后，硬删其业务与消息内容残留，user 记录保留。

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
        # 硬删其 conversation_log 残留
        try:
            total_deleted += _batch_hard_delete(
                db,
                "conversation_log",
                f"userid = {_escape_literal(uid)}",
            )
        except Exception:
            logger.exception(
                "ttl_cleanup: hard delete conversation_log failed user_hash={}",
                identifier_hash(uid),
            )
        # outbox 同样含原始回复文本和用户标识，必须纳入删除用户的数据清除边界。
        try:
            total_deleted += _batch_hard_delete(
                db,
                "wecom_outbound_outbox",
                f"userid = {_escape_literal(uid)}",
            )
        except Exception:
            logger.exception(
                "ttl_cleanup: hard delete outbox failed user_hash={}",
                identifier_hash(uid),
            )
        # inbound_event 也含用户标识和文本摘要。仅删除终态记录；若仍有可恢复
        # 状态则保留并交给 worker/告警处理，避免合规清理制造业务半提交。
        try:
            total_deleted += _batch_hard_delete(
                db,
                "wecom_inbound_event",
                f"from_userid = {_escape_literal(uid)} "
                "AND status IN ('done','dead_letter')",
            )
        except Exception:
            logger.exception(
                "ttl_cleanup: hard delete inbound failed user_hash={}",
                identifier_hash(uid),
            )

    log_event("ttl_cleanup_deleted_users", userid_count=len(userids), rows_deleted=total_deleted)
    return total_deleted


def _hard_delete_terminal_inbound(db, retention_days: int) -> int:
    """Delete only fully recoverable terminal events past retention.

    ``received``/``processing``/``failed`` and ``session_pending`` are durable
    recovery sources and must never disappear because of age alone. A ``done``
    event also remains the visibility gate for its transactional outbox, so keep
    it while any reply is still pending or sending.
    """
    days = int(retention_days)
    return _batch_hard_delete(
        db,
        "wecom_inbound_event",
        "status IN ('done','dead_letter') "
        f"AND created_at < NOW() - INTERVAL {days} DAY "
        "AND NOT EXISTS ("
        "SELECT 1 FROM wecom_outbound_outbox o "
        "WHERE o.inbound_event_id = wecom_inbound_event.id "
        "AND o.status IN ('pending','sending')"
        ")",
    )


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
            # 旧库升级兜底：读取配置前先自愈补齐缺失 key。
            # scheduler.start() 启动时已经跑过一次；这里再跑一次是纯兜底，
            # 针对"app 启动后被手工 DELETE system_config 行"或"直接手工调 run()"场景。
            try:
                ensure_ttl_config_defaults(db)
            except Exception:
                logger.exception("ttl_cleanup: ensure_ttl_config_defaults failed (non-fatal)")

            # Phase 7：天数全量从 system_config 读取，失败兜底默认值
            cfg = _load_ttl_config(db)
            stats["_config"] = cfg  # 记录本次使用的配置，便于事后复盘
            delay = cfg["hard_delete_delay_days"]
            conv_days = cfg["conversation_log_days"]
            inbound_days = cfg["wecom_inbound_event_days"]
            audit_days = cfg["audit_log_days"]

            # 90 天明细留存独立读取，不并入 _load_ttl_config 的 4 个通用 key。
            recommendation_days = _read_int_config(
                db,
                "ttl.recommendation_detail.days",
                RECOMMENDATION_DETAIL_RETENTION_DAYS,
            )

            _safe_step("soft_delete_jobs", stats, lambda: _soft_delete_expired_jobs(db))
            _safe_step(
                "redact_recommendation_content",
                stats,
                lambda: _redact_expired_recommendation_content(db),
            )
            _safe_step(
                "purge_recommendation_details",
                stats,
                lambda: _purge_expired_recommendation_details(
                    db, recommendation_days,
                ),
            )
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
                "hard_delete_outbox_sent",
                stats,
                lambda: _batch_hard_delete(
                    db, "wecom_outbound_outbox",
                    "status='sent' AND "
                    f"created_at < NOW() - INTERVAL {int(inbound_days)} DAY",
                ),
            )
            _safe_step(
                "hard_delete_outbox_dead_letter",
                stats,
                lambda: _batch_hard_delete(
                    db, "wecom_outbound_outbox",
                    "status='dead_letter' AND "
                    f"created_at < NOW() - INTERVAL {int(audit_days)} DAY",
                ),
            )
            _safe_step(
                "hard_delete_inbound",
                stats,
                lambda: _hard_delete_terminal_inbound(db, inbound_days),
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
