"""APScheduler 注册入口（随 app 进程启动）。

关键约束（对齐 phase7-main.md §3.1 模块 A）：
- 使用 `BackgroundScheduler`（非 `BlockingScheduler`），不阻塞 FastAPI 主线程。
- 所有任务 `max_instances=1 + coalesce=True`，防止同一 app 实例内堆叠。
- app 横向扩容时，由各任务内部的 `task_lock` 分布式锁保证单实例执行。
- 先 `scheduler.start()` 再读 `job.next_run_time`；pending 状态的 job 在 start 前
  没有 next_run_time，直接访问会 `AttributeError`。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from app.config import settings

_scheduler: BackgroundScheduler | None = None


def build_scheduler() -> BackgroundScheduler:
    """构造 BackgroundScheduler 并注册所有任务。"""
    # 延迟 import，避免 app 启动时的循环依赖
    from app.tasks import (
        daily_report,
        domain_outbox_consumer,
        job_candidate_cleanup,
        job_expiry_cleanup,
        media_cleanup_worker,
        recommendation_privacy_cleanup,
        resume_candidate_cleanup,
        resume_expiry_cleanup,
        send_retry_drain,
        target_cleanup_worker,
        ttl_cleanup,
        worker_monitor,
    )

    sched = BackgroundScheduler(timezone=settings.scheduler_timezone)

    # ---- 每日 02:30 推荐域延迟硬删（§9.11.1） ----
    # 必须排在 ttl_cleanup 之前：闭环第 1 步要从 resume/job 读该用户的候选 target ID，
    # 而 ttl_cleanup 会把这些行硬删掉，顺序反了就再也反查不到相关 impression。
    sched.add_job(
        recommendation_privacy_cleanup.run,
        CronTrigger.from_crontab("30 2 * * *"),
        id="recommendation_privacy_cleanup",
        max_instances=1,
        coalesce=True,
    )

    # ---- 每日 03:00 TTL 清理与硬删除 ----
    sched.add_job(
        ttl_cleanup.run,
        CronTrigger.from_crontab("0 3 * * *"),
        id="ttl_cleanup",
        max_instances=1,
        coalesce=True,
    )

    sched.add_job(
        media_cleanup_worker.run,
        IntervalTrigger(minutes=1),
        id="media_cleanup_worker",
        max_instances=1,
        coalesce=True,
    )

    sched.add_job(
        target_cleanup_worker.run,
        IntervalTrigger(minutes=1),
        id="target_cleanup_worker",
        max_instances=1,
        coalesce=True,
    )

    sched.add_job(
        job_candidate_cleanup.run,
        IntervalTrigger(minutes=10),
        id="job_candidate_cleanup",
        max_instances=1,
        coalesce=True,
    )

    sched.add_job(
        job_expiry_cleanup.run,
        IntervalTrigger(minutes=10),
        id="job_expiry_cleanup",
        max_instances=1,
        coalesce=True,
    )

    sched.add_job(
        resume_candidate_cleanup.run, IntervalTrigger(minutes=10),
        id="resume_candidate_cleanup", max_instances=1, coalesce=True,
    )
    sched.add_job(
        resume_expiry_cleanup.run, IntervalTrigger(minutes=10),
        id="resume_expiry_cleanup", max_instances=1, coalesce=True,
    )

    # ---- 每日 09:00 企微群日报 ----
    sched.add_job(
        daily_report.run,
        CronTrigger.from_crontab("0 9 * * *"),
        id="daily_report",
        max_instances=1,
        coalesce=True,
    )

    # ---- Worker 心跳巡检（180s） ----
    sched.add_job(
        worker_monitor.check_heartbeat,
        IntervalTrigger(seconds=180),
        id="heartbeat",
        max_instances=1,
        coalesce=True,
    )

    # ---- 入队积压巡检（60s） ----
    sched.add_job(
        worker_monitor.check_queue_backlog,
        IntervalTrigger(seconds=60),
        id="queue_backlog",
        max_instances=1,
        coalesce=True,
    )

    # ---- 死信队列巡检（60s） ----
    sched.add_job(
        worker_monitor.check_dead_letter,
        IntervalTrigger(seconds=60),
        id="dead_letter",
        max_instances=1,
        coalesce=True,
    )

    # ---- durable outbox 死信/超龄巡检（60s） ----
    sched.add_job(
        worker_monitor.check_outbox,
        IntervalTrigger(seconds=60),
        id="outbox_health",
        max_instances=1,
        coalesce=True,
    )

    sched.add_job(
        worker_monitor.check_media_cleanup,
        IntervalTrigger(seconds=60),
        id="media_cleanup_health",
        max_instances=1,
        coalesce=True,
    )

    sched.add_job(
        worker_monitor.check_session_commits,
        IntervalTrigger(seconds=60),
        id="session_commit_health",
        max_instances=1,
        coalesce=True,
    )

    # ---- Phase 14 domain outbox projection consumer (30s) ----
    # The task is registered in every app process; its distributed lease and
    # feature switch keep disabled/duplicate instances harmless.
    sched.add_job(
        domain_outbox_consumer.run,
        IntervalTrigger(seconds=30),
        id="domain_outbox_consumer",
        max_instances=1,
        coalesce=True,
    )

    # ---- Action execution lease/reference/replay observation (60s) ----
    sched.add_job(
        worker_monitor.check_action_execution,
        IntervalTrigger(seconds=60),
        id="action_execution_health",
        max_instances=1,
        coalesce=True,
    )

    # ---- 推荐曝光日聚合校验/重算（§11.8） ----
    # 每天 00:15 全量对账昨日；再叠一个小时级增量，避免整整一天的偏差要等到次日才
    # 被发现。两个 job 共用任务内部的 task_lock，重叠时后者直接跳过。
    from app.tasks import recommendation_exposure_reconcile

    sched.add_job(
        recommendation_exposure_reconcile.run,
        CronTrigger.from_crontab("15 0 * * *"),
        id="recommendation_exposure_reconcile",
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        recommendation_exposure_reconcile.run,
        IntervalTrigger(hours=1),
        args=[1],
        id="recommendation_exposure_reconcile_intraday",
        max_instances=1,
        coalesce=True,
    )

    # ---- 出站补偿队列长度巡检（600s） ----
    sched.add_job(
        send_retry_drain.check_backlog,
        IntervalTrigger(seconds=600),
        id="send_retry_check",
        max_instances=1,
        coalesce=True,
    )

    # ---- 群消息重试消费（30s） ----
    sched.add_job(
        send_retry_drain.drain_group_send_retry,
        IntervalTrigger(seconds=30),
        id="group_send_drain",
        max_instances=1,
        coalesce=True,
    )

    return sched


def schedule_job_expiry_continuation() -> bool:
    """Schedule one immediate follow-up without waiting for the next interval tick."""
    if _scheduler is None:
        logger.warning("job expiry continuation skipped: scheduler not running")
        return False
    from app.tasks import job_expiry_cleanup
    _scheduler.add_job(
        job_expiry_cleanup.run,
        DateTrigger(run_date=datetime.now(timezone.utc) + timedelta(seconds=5)),
        id="job_expiry_cleanup_continuation",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    return True


def schedule_job_candidate_continuation() -> bool:
    """Schedule candidate cleanup follow-up without waiting for the next interval."""
    if _scheduler is None:
        logger.warning("job candidate continuation skipped: scheduler not running")
        return False
    from app.tasks import job_candidate_cleanup
    _scheduler.add_job(
        job_candidate_cleanup.run,
        DateTrigger(run_date=datetime.now(timezone.utc) + timedelta(seconds=5)),
        id="job_candidate_cleanup_continuation",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    return True


def schedule_resume_expiry_continuation() -> bool:
    if _scheduler is None:
        logger.warning("resume expiry continuation skipped: scheduler not running")
        return False
    from app.tasks import resume_expiry_cleanup
    _scheduler.add_job(
        resume_expiry_cleanup.run,
        DateTrigger(run_date=datetime.now(timezone.utc) + timedelta(seconds=5)),
        id="resume_expiry_cleanup_continuation", replace_existing=True,
        max_instances=1, coalesce=True,
    )
    return True


def schedule_resume_candidate_continuation() -> bool:
    if _scheduler is None:
        logger.warning("resume candidate continuation skipped: scheduler not running")
        return False
    from app.tasks import resume_candidate_cleanup
    _scheduler.add_job(
        resume_candidate_cleanup.run,
        DateTrigger(run_date=datetime.now(timezone.utc) + timedelta(seconds=5)),
        id="resume_candidate_cleanup_continuation", replace_existing=True,
        max_instances=1, coalesce=True,
    )
    return True


def start() -> None:
    """启动调度器；重复调用是安全的。"""
    global _scheduler
    if _scheduler is not None:
        return

    # 旧库升级自愈：Phase 7 新增的 ttl.* system_config key 若缺失，在此幂等补齐。
    # 首次部署走 seed.sql 不会触发；已上线环境没跑 phase7_001 迁移时会补齐并 warn。
    _self_heal_ttl_config()

    _scheduler = build_scheduler()

    # start 前只能读 id / trigger；pending job 的 next_run_time 尚未计算。
    for job in _scheduler.get_jobs():
        logger.info(f"scheduler pending: id={job.id} trigger={job.trigger}")

    _scheduler.start()

    # start 后 next_run_time 才可用。
    for job in _scheduler.get_jobs():
        logger.info(f"scheduler running: id={job.id} next_run={job.next_run_time}")


def shutdown() -> None:
    """lifespan shutdown 时调用；`wait=False` 避免阻塞退出。"""
    global _scheduler
    if _scheduler is None:
        return
    _scheduler.shutdown(wait=False)
    _scheduler = None
    logger.info("scheduler stopped")


def _self_heal_ttl_config() -> None:
    """启动前一次性补齐缺失的 ttl.* system_config key（Phase 7 §0.1 U2）。

    独立函数而非内联，方便单测 mock。任何异常都只 warn，不阻塞 app 启动：
    数据库未就绪是更上层的健康检查问题，不应被这里连累成启动失败。
    """
    try:
        from app.db import SessionLocal
        from app.tasks.common import ensure_ttl_config_defaults

        with SessionLocal() as db:
            added = ensure_ttl_config_defaults(db)
        if added:
            logger.warning(
                f"scheduler: self-healed {added} missing ttl.* system_config key(s); "
                "run phase7_001 migration on next maintenance window"
            )
    except Exception:
        logger.exception("scheduler: _self_heal_ttl_config failed (non-fatal)")
