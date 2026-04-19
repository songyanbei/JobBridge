"""APScheduler 注册入口（随 app 进程启动）。

关键约束（对齐 phase7-main.md §3.1 模块 A）：
- 使用 `BackgroundScheduler`（非 `BlockingScheduler`），不阻塞 FastAPI 主线程。
- 所有任务 `max_instances=1 + coalesce=True`，防止同一 app 实例内堆叠。
- app 横向扩容时，由各任务内部的 `task_lock` 分布式锁保证单实例执行。
- 先 `scheduler.start()` 再读 `job.next_run_time`；pending 状态的 job 在 start 前
  没有 next_run_time，直接访问会 `AttributeError`。
"""
from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from app.config import settings

_scheduler: BackgroundScheduler | None = None


def build_scheduler() -> BackgroundScheduler:
    """构造 BackgroundScheduler 并注册所有任务。"""
    # 延迟 import，避免 app 启动时的循环依赖
    from app.tasks import daily_report, send_retry_drain, ttl_cleanup, worker_monitor

    sched = BackgroundScheduler(timezone=settings.scheduler_timezone)

    # ---- 每日 03:00 TTL 清理与硬删除 ----
    sched.add_job(
        ttl_cleanup.run,
        CronTrigger.from_crontab("0 3 * * *"),
        id="ttl_cleanup",
        max_instances=1,
        coalesce=True,
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
