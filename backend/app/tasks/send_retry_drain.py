"""出站补偿巡检 + 群消息重试消费任务（Phase 7 §3.1 模块 F/H）。

职责分工：
- ``check_backlog``（600s）：检查 ``queue:send_retry`` + ``queue:group_send_retry`` 长度，
  超过阈值触发告警。**本任务只做监控**，不消费 ``queue:send_retry``（Phase 4 Worker 已消费）。
- ``drain_group_send_retry``（30s）：每次消费一条 ``queue:group_send_retry``，
  按 60s / 120s / 300s 指数退避重试；3 次失败后 loguru ``group_send_failed_final`` 丢弃。
- ``enqueue_group_send_retry``：模块 E / daily_report 推送失败时调用的入队函数。

payload 结构（phase7-main.md §5.3）::

    {
      "chat_id": "...",
      "content": "...",
      "retry_count": 0,
      "backoff_until": 1712000000.0
    }
"""
from __future__ import annotations

import json
import time
from typing import Any

from loguru import logger

from app.config import settings
from app.core.redis_client import (
    QUEUE_GROUP_SEND_RETRY,
    QUEUE_SEND_RETRY,
    get_redis,
)
from app.tasks.common import log_event, task_lock

SEND_RETRY_BACKOFFS = [60, 120, 300]  # 秒，指数退避
MAX_GROUP_RETRY = 3


# ---------------------------------------------------------------------------
# 入队辅助
# ---------------------------------------------------------------------------

def enqueue_group_send_retry(
    chat_id: str,
    content: str,
    retry_count: int = 0,
    backoff: int = 60,
) -> None:
    """把群消息放入 ``queue:group_send_retry``，待 ``drain_group_send_retry`` 消费。

    Args:
        chat_id: 企微群 chatid。
        content: 文本内容。
        retry_count: 已失败次数（首次入队 0）。
        backoff: 最早允许重试的秒数（从现在算起）。
    """
    payload = {
        "chat_id": chat_id,
        "content": content,
        "retry_count": retry_count,
        "backoff_until": time.time() + backoff,
    }
    try:
        get_redis().rpush(
            QUEUE_GROUP_SEND_RETRY, json.dumps(payload, ensure_ascii=False)
        )
    except Exception:
        logger.exception("enqueue_group_send_retry failed: chat_id=%s", chat_id)


# ---------------------------------------------------------------------------
# 巡检：两个出站队列的长度
# ---------------------------------------------------------------------------

def check_backlog() -> None:
    """``queue:send_retry`` + ``queue:group_send_retry`` 长度巡检。"""
    with task_lock("send_retry_drain.check_backlog", ttl=300) as acquired:
        if not acquired:
            return
        try:
            r = get_redis()
            threshold = settings.monitor_send_retry_threshold
            for queue_key in (QUEUE_SEND_RETRY, QUEUE_GROUP_SEND_RETRY):
                n = int(r.llen(queue_key) or 0)
                if n > threshold:
                    _alert(
                        f"send_retry_backlog:{queue_key}",
                        f"⚠️ {queue_key} 积压 {n} 条，阈值 {threshold}",
                    )
        except Exception:
            logger.exception("send_retry_drain.check_backlog failed")


# ---------------------------------------------------------------------------
# 消费 queue:group_send_retry
# ---------------------------------------------------------------------------

def drain_group_send_retry() -> None:
    """每 30 秒从 ``queue:group_send_retry`` 取一条尝试重发。

    未到 backoff_until → 放回队尾；成功 → 结束；失败 → retry_count+1 + 重新入队。
    超过 ``MAX_GROUP_RETRY`` 次后写 loguru ``group_send_failed_final`` 并丢弃。
    """
    with task_lock("group_send_drain", ttl=60) as acquired:
        if not acquired:
            return

        try:
            r = get_redis()
            raw = r.lpop(QUEUE_GROUP_SEND_RETRY)
            if not raw:
                return

            try:
                payload = json.loads(raw)
            except Exception:
                log_event("group_send_invalid_payload", raw=str(raw)[:200])
                return

            now = time.time()
            if now < float(payload.get("backoff_until", 0) or 0):
                r.rpush(QUEUE_GROUP_SEND_RETRY, json.dumps(payload, ensure_ascii=False))
                return

            chat_id = payload.get("chat_id", "")
            content = payload.get("content", "")
            retry_count = int(payload.get("retry_count", 0) or 0)

            from app.wecom.client import WeComClient
            client = WeComClient()
            ok = client.send_text_to_group(chat_id, content)

            if ok:
                log_event("group_send_retry_success", retry_count=retry_count, chat_id=chat_id)
                return

            retry_count += 1
            if retry_count >= MAX_GROUP_RETRY:
                log_event(
                    "group_send_failed_final",
                    retry_count=retry_count,
                    chat_id=chat_id,
                )
                return

            idx = min(retry_count - 1, len(SEND_RETRY_BACKOFFS) - 1)
            next_backoff = SEND_RETRY_BACKOFFS[idx]
            payload["retry_count"] = retry_count
            payload["backoff_until"] = time.time() + next_backoff
            r.rpush(QUEUE_GROUP_SEND_RETRY, json.dumps(payload, ensure_ascii=False))
        except Exception:
            logger.exception("drain_group_send_retry failed")


# ---------------------------------------------------------------------------
# 与 worker_monitor 共用的告警通道（避免循环 import，独立实现精简版）
# ---------------------------------------------------------------------------

def _alert(event: str, message: str) -> None:
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
    if not first_time:
        return

    chat_id = (settings.daily_report_chat_id or "").strip()
    if not chat_id:
        return
    from app.wecom.client import WeComClient
    client = WeComClient()
    if not client.send_text_to_group(chat_id, message):
        # 推送也失败 → 入群消息重试队列
        enqueue_group_send_retry(chat_id, message)
