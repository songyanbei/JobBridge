"""Worker 心跳 / 队列 / 死信巡检任务（Phase 7 §3.1 模块 E）。

三个独立检查函数（分别被 APScheduler 以不同间隔调起）：
- ``check_heartbeat``（180s）：扫描 ``worker:heartbeat:*`` 无任一有效 key 即告警
- ``check_queue_backlog``（60s）：``LLEN queue:incoming > 阈值`` 告警
- ``check_dead_letter``（60s）：``LLEN queue:dead_letter > 0`` 立即告警

告警去重：同一 event 10 分钟内只推送一次企微群（``alert_dedupe:{event}`` SETNX EX N）。
阈值从 ``.env`` 读取（``MONITOR_*``），不进 admin 配置页（对齐 §13.3）。
"""
from __future__ import annotations

from loguru import logger

from app.config import settings
from app.core.redis_client import QUEUE_DEAD_LETTER, QUEUE_INCOMING, get_redis
from app.tasks.common import log_event, task_lock


# ---------------------------------------------------------------------------
# 巡检函数
# ---------------------------------------------------------------------------

def check_heartbeat() -> None:
    """扫描 ``worker:heartbeat:*`` 是否有任一活跃 key。

    Phase 4 Worker 每 60s 写一次 TTL=120s 的心跳。在正常运行时始终应有至少一个 key。
    全部失联视为 Worker 进程全部离线，触发告警。

    ``task_lock`` TTL 设 120s 覆盖 180s 巡检间隔：Lua CAS 在任务完成立即释放锁，
    TTL 仅作进程崩溃兜底；既实现单实例执行又不会跨间隔残留。
    """
    with task_lock("worker_monitor.heartbeat", ttl=120) as acquired:
        if not acquired:
            return
        try:
            r = get_redis()
            # scan_iter 比 KEYS * 对生产更友好
            keys = [k for k in r.scan_iter(match="worker:heartbeat:*", count=100)]
            if not keys:
                _alert("worker_all_offline", "❗ Worker 全部离线或心跳未上报，请立即排查")
            else:
                log_event("worker_heartbeat_ok", worker_count=len(keys))
        except Exception:
            logger.exception("check_heartbeat failed")


def check_queue_backlog() -> None:
    """``queue:incoming`` 长度超过阈值触发告警。"""
    with task_lock("worker_monitor.queue_backlog", ttl=45) as acquired:
        if not acquired:
            return
        try:
            r = get_redis()
            n = int(r.llen(QUEUE_INCOMING) or 0)
            threshold = settings.monitor_queue_incoming_threshold
            if n > threshold:
                _alert(
                    "queue_backlog",
                    f"⚠️ {QUEUE_INCOMING} 积压 {n} 条，阈值 {threshold}",
                )
        except Exception:
            logger.exception("check_queue_backlog failed")


def check_dead_letter() -> None:
    """``queue:dead_letter`` 存在任何消息立即告警。"""
    with task_lock("worker_monitor.dead_letter", ttl=45) as acquired:
        if not acquired:
            return
        try:
            r = get_redis()
            n = int(r.llen(QUEUE_DEAD_LETTER) or 0)
            if n > 0:
                _alert(
                    "dead_letter",
                    f"❗ {QUEUE_DEAD_LETTER} 存在 {n} 条死信，需人工处理",
                )
        except Exception:
            logger.exception("check_dead_letter failed")


# ---------------------------------------------------------------------------
# 告警（含去重 + 企微群推送）
# ---------------------------------------------------------------------------

def _alert(event: str, message: str) -> None:
    """带去重的告警发送。

    - loguru 每次都记录（便于运维按时间线排查）。
    - 首次命中 dedupe 窗口才真正推送企微群；窗口内后续告警仅打日志。
    """
    r = get_redis()
    dedupe_key = f"alert_dedupe:{event}"
    first_time = bool(
        r.set(
            dedupe_key,
            "1",
            nx=True,
            ex=settings.monitor_alert_dedupe_seconds,
        )
    )
    log_event(event, message=message, first_time=first_time)
    if first_time:
        _push_group(message)


def _push_group(content: str) -> None:
    """推送告警到企微运营群；失败入 queue:group_send_retry。"""
    chat_id = (settings.daily_report_chat_id or "").strip()
    if not chat_id:
        log_event("alert_push_skipped", reason="chat_id_empty")
        return

    # 延迟 import 避免循环依赖（wecom.client → config → tasks）
    from app.wecom.client import WeComClient
    from app.tasks.send_retry_drain import enqueue_group_send_retry

    client = WeComClient()
    ok = client.send_text_to_group(chat_id, content)
    if not ok:
        enqueue_group_send_retry(chat_id, content)
