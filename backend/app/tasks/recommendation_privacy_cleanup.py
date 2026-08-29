"""推荐域延迟硬删任务（方案 §9.11.1 / §10.1.1 / §14.12）。

``/删除我的信息`` 命令当场只做正文脱敏（``user_service.delete_user_data`` →
``recommendation_privacy_service.redact_user_recommendation_content``）。真正的
事实行删除等 ``ttl.hard_delete.delay_days`` 天后由本任务执行。

**必须排在 ``ttl_cleanup.run()` 之前**：§9.11.1 步骤 1 要先从 ``resume``/``job``
读出该用户的候选 target ID，而 ``ttl_cleanup`` 到期后会把这些行硬删掉；顺序反了
就再也反查不到「别人看到过我」的 impression / exposure_daily。

约束：
- 用 ``task_lock`` 保证多实例只有一个在跑；
- 每个用户一个随机批次 ID，日志只写批次 ID、步骤、表名和行数；
- 单个用户失败不影响其它用户，失败进入 ``recommendation_privacy_service`` 的
  可观测重试队列（§10.1.1 行 2240）。
"""
from __future__ import annotations

from loguru import logger
from sqlalchemy import text

from app.db import SessionLocal
from app.services import recommendation_privacy_service as privacy
from app.services.lifecycle_config_service import get_hard_delete_delay_days
from app.tasks.common import log_event, task_lock

# 一轮最多处理多少个用户，避免单次任务无限拉长。
MAX_USERS_PER_RUN = 200
# 一轮最多重放多少条失败任务。
MAX_RETRY_JOBS_PER_RUN = 100

DEFAULT_DELAY_DAYS = 7


def _read_delay_days(db) -> int:
    """复用 ``ttl.hard_delete.delay_days``，与 ttl_cleanup 保持同一个倒计时。"""
    return get_hard_delete_delay_days(db)


def due_userids(db, delay_days: int, limit: int = MAX_USERS_PER_RUN) -> list[str]:
    """已过延迟期、等待推荐域硬删的用户。

    倒计时口径与 ``ttl_cleanup._hard_delete_deleted_users`` 完全一致：
    主用 ``user.extra['deleted_at']``（UTC 字符串）比 ``UTC_TIMESTAMP()``；
    没有该字段时兜底用最后一条 ``/删除我的信息`` 审计记录比 ``NOW()``
    （两者同一个 server time zone，绕开 ``CONVERT_TZ(SYSTEM)`` 返回 NULL 的坑）。
    """
    safe_delay = int(delay_days)
    safe_limit = int(limit)
    rows = db.execute(
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
                STR_TO_DATE(
                  JSON_UNQUOTE(JSON_EXTRACT(user.extra, '$.deleted_at')),
                  '%Y-%m-%d %H:%i:%s'
                ) < UTC_TIMESTAMP() - INTERVAL {safe_delay} DAY
                OR (
                  JSON_EXTRACT(user.extra, '$.deleted_at') IS NULL
                  AND al.last_delete_at IS NOT NULL
                  AND al.last_delete_at < NOW() - INTERVAL {safe_delay} DAY
                )
              )
            LIMIT {safe_limit}
            """
        )
    ).fetchall()
    return [row[0] for row in rows]


def _run_one(db, external_userid: str, *, attempt: int = 0) -> privacy.PrivacyReport:
    """跑一个用户的完整闭环；失败自动进重试队列。"""
    report = privacy.delete_recommendation_user_data(db, external_userid, commit=True)
    if not report.ok:
        privacy.enqueue_privacy_retry(
            external_userid,
            batch_id=report.batch_id,
            failed_steps=report.failed_steps,
            attempt=attempt,
        )
    return report


def drain_retry_queue(db, limit: int = MAX_RETRY_JOBS_PER_RUN) -> dict[str, int]:
    """重放失败批次。闭环幂等，整体重跑即可。"""
    replayed = 0
    recovered = 0
    for _ in range(int(limit)):
        job = privacy.pop_privacy_retry()
        if job is None:
            break
        userid = job.get("userid")
        if not isinstance(userid, str) or not userid:
            continue
        replayed += 1
        report = _run_one(db, userid, attempt=int(job.get("attempt") or 0))
        if report.ok:
            recovered += 1
    return {"replayed": replayed, "recovered": recovered}


def run() -> None:
    """APScheduler 调起的任务入口；建议每日一次，排在 ``ttl_cleanup`` 之前。"""
    with task_lock("recommendation_privacy_cleanup", ttl=3600) as acquired:
        if not acquired:
            logger.info(
                "recommendation_privacy_cleanup: skipped, another instance holds the lock",
            )
            return

        stats: dict = {"users": 0, "ok": 0, "failed": 0, "rows": 0}
        with SessionLocal() as db:
            try:
                delay = _read_delay_days(db)
                userids = due_userids(db, delay)
            except Exception:
                logger.exception("recommendation_privacy_cleanup: candidate scan failed")
                userids = []
                delay = DEFAULT_DELAY_DAYS

            stats["delay_days"] = delay
            for userid in userids:
                stats["users"] += 1
                try:
                    report = _run_one(db, userid)
                except Exception:
                    # 兜底：闭环内部已逐步 try/except，这里只防调用层异常。
                    logger.exception("recommendation_privacy_cleanup: closure crashed")
                    stats["failed"] += 1
                    privacy.enqueue_privacy_retry(
                        userid, batch_id="crashed", failed_steps=["closure"],
                    )
                    continue
                stats["rows"] += report.total_rows
                stats["ok" if report.ok else "failed"] += 1

            try:
                stats.update(drain_retry_queue(db))
            except Exception:
                logger.exception("recommendation_privacy_cleanup: retry drain failed")

        stats["retry_depth"] = privacy.privacy_retry_depth()
        log_event("recommendation_privacy_cleanup_summary", **stats)
