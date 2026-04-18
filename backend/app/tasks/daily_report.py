"""每日 09:00 企微群日报任务（Phase 7 §3.1 模块 D）。

数据口径对齐 §13.5 / §17.1，主力走 ``services/report_service.get_dashboard()``，
补充两项不在 dashboard 中的字段：
- 审核打回率：当日 audit_log(action in ('manual_reject','auto_reject')) / 当日总审核动作数
- Worker 健康 / 队列 / 死信：Redis 实时值

推送：``WeComClient.send_text_to_group(DAILY_REPORT_CHAT_ID, content)``；
失败入 ``queue:group_send_retry``；chat_id 为空时只打 loguru 不报错。
不写 audit_log（对齐 §0 本版修订）。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import func

from app.config import settings
from app.core.redis_client import (
    QUEUE_DEAD_LETTER,
    QUEUE_GROUP_SEND_RETRY,
    QUEUE_INCOMING,
    QUEUE_SEND_RETRY,
    get_redis,
)
from app.db import SessionLocal
from app.models import AuditLog, User
from app.services import report_service
from app.tasks.common import log_event, task_lock
from app.tasks.send_retry_drain import enqueue_group_send_retry


REJECT_ACTIONS = ("manual_reject", "auto_reject")
ALL_AUDIT_ACTIONS = (
    "auto_pass",
    "auto_reject",
    "manual_pass",
    "manual_reject",
    "manual_edit",
)


# ---------------------------------------------------------------------------
# 任务入口
# ---------------------------------------------------------------------------

def run() -> None:
    """APScheduler 调起的日报入口。"""
    with task_lock("daily_report", ttl=600) as acquired:
        if not acquired:
            logger.info("daily_report: skipped, another instance holds the lock")
            return

        try:
            content = _compose_report()
        except Exception:
            logger.exception("daily_report: compose failed")
            return

        chat_id = (settings.daily_report_chat_id or "").strip()
        if not chat_id:
            log_event("daily_report_skipped_no_chat_id", content_length=len(content))
            return

        from app.wecom.client import WeComClient
        client = WeComClient()
        ok = client.send_text_to_group(chat_id, content)
        log_event("daily_report_generated", pushed=ok, length=len(content))

        if not ok:
            enqueue_group_send_retry(chat_id, content)


# ---------------------------------------------------------------------------
# 报文组装
# ---------------------------------------------------------------------------

def _compose_report() -> str:
    """生成日报文本。"""
    today = date.today()
    yesterday = today - timedelta(days=1)

    with SessionLocal() as db:
        dashboard = report_service.get_dashboard(db, force_refresh=True)
        today_metrics = dashboard.get("today", {}) or {}
        yesterday_metrics = dashboard.get("yesterday", {}) or {}

        audit_reject_today, total_audit_today = _audit_reject_rate(db, today)
        audit_reject_yest, total_audit_yest = _audit_reject_rate(db, yesterday)

        new_blocked_today = _new_blocked_count(db, today)

    # Redis 实时值
    try:
        r = get_redis()
        incoming_len = int(r.llen(QUEUE_INCOMING) or 0)
        dead_letter_len = int(r.llen(QUEUE_DEAD_LETTER) or 0)
        send_retry_len = int(r.llen(QUEUE_SEND_RETRY) or 0)
        group_retry_len = int(r.llen(QUEUE_GROUP_SEND_RETRY) or 0)
        heartbeat_keys = [k for k in r.scan_iter(match="worker:heartbeat:*", count=100)]
        worker_healthy = len(heartbeat_keys) > 0
        worker_count = len(heartbeat_keys)
    except Exception:
        logger.exception("daily_report: redis snapshot failed")
        incoming_len = dead_letter_len = send_retry_len = group_retry_len = 0
        worker_healthy = False
        worker_count = 0

    # 昨日对比
    def _delta(cur: float, prev: float, pct: bool = False) -> str:
        if prev is None:
            return ""
        diff = cur - prev
        arrow = "↑" if diff > 0 else ("↓" if diff < 0 else "±")
        if pct:
            return f"（{arrow} {abs(diff) * 100:.1f}%）"
        return f"（{arrow} {abs(diff):.0f}）"

    dau_today = int(today_metrics.get("dau_total") or 0)
    dau_yest = int(yesterday_metrics.get("dau_total") or 0)

    uploads_job_today = int(today_metrics.get("uploads_job") or 0)
    uploads_resume_today = int(today_metrics.get("uploads_resume") or 0)

    search_today = int(today_metrics.get("search_count") or 0)
    search_yest = int(yesterday_metrics.get("search_count") or 0)

    hit_today = float(today_metrics.get("hit_rate") or 0)
    hit_yest = float(yesterday_metrics.get("hit_rate") or 0)
    empty_today = float(today_metrics.get("empty_recall_rate") or 0)

    reject_today_pct = (audit_reject_today / total_audit_today) if total_audit_today else 0.0
    reject_yest_pct = (audit_reject_yest / total_audit_yest) if total_audit_yest else 0.0

    audit_pending = int(today_metrics.get("audit_pending") or 0)

    lines = [
        f"📊 JobBridge 日报 {today.isoformat()}",
        "",
        f"DAU：{dau_today}{_delta(dau_today, dau_yest)}",
        f"上传：岗位 {uploads_job_today} / 简历 {uploads_resume_today}",
        f"检索：{search_today} 次{_delta(search_today, search_yest)}",
        f"命中率：{hit_today * 100:.1f}%{_delta(hit_today, hit_yest, pct=True)}",
        f"空召回率：{empty_today * 100:.1f}%",
        f"审核打回率：{reject_today_pct * 100:.1f}%{_delta(reject_today_pct, reject_yest_pct, pct=True)}",
        f"新增封禁：{new_blocked_today}",
        "",
        "⚙️ 运行",
        f"待审积压：{audit_pending}",
        f"入队：{incoming_len} / 出站重试：{send_retry_len} / 群重试：{group_retry_len}",
        f"死信数：{dead_letter_len}",
        f"Worker 健康：{'✅ ' + str(worker_count) if worker_healthy else '❌ 全部离线'}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 补充指标查询
# ---------------------------------------------------------------------------

def _audit_reject_rate(db, d: date) -> tuple[int, int]:
    """返回 (当日打回条数, 当日审核动作总数)。

    打回定义：``action IN ('manual_reject', 'auto_reject')``。
    分母用所有常规审核动作，避免受 ``undo`` / ``appeal`` 等非业务流程稀释。
    """
    start = datetime.combine(d, time.min)
    end = start + timedelta(days=1)
    total = int(
        db.query(func.count(AuditLog.id))
        .filter(
            AuditLog.created_at >= start,
            AuditLog.created_at < end,
            AuditLog.action.in_(ALL_AUDIT_ACTIONS),
        )
        .scalar()
        or 0
    )
    reject = int(
        db.query(func.count(AuditLog.id))
        .filter(
            AuditLog.created_at >= start,
            AuditLog.created_at < end,
            AuditLog.action.in_(REJECT_ACTIONS),
        )
        .scalar()
        or 0
    )
    return reject, total


def _new_blocked_count(db, d: date) -> int:
    """当日新增封禁数。

    约束：Phase 1 audit_log.action 是封闭枚举，没有专门的 "block" 动作；
    封禁流程通过 ``manual_edit`` / ``manual_reject`` + reason 文案表达。
    这里必须同时满足 ``target_type='user'`` 且 reason 含"封禁"或"blocked"关键字，
    避免把 user 的任意人工审核编辑都算进去（偏高失真）。
    二期建议在 audit_log.action 增加专门的 block/unblock 动作。
    """
    start = datetime.combine(d, time.min)
    end = start + timedelta(days=1)
    return int(
        db.query(func.count(AuditLog.id))
        .filter(
            AuditLog.created_at >= start,
            AuditLog.created_at < end,
            AuditLog.target_type == "user",
            AuditLog.action.in_(("manual_edit", "manual_reject")),
            AuditLog.reason.isnot(None),
            (AuditLog.reason.like("%封禁%") | AuditLog.reason.like("%blocked%")),
        )
        .scalar()
        or 0
    )
