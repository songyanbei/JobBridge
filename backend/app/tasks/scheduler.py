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
