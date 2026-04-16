"""异步 Worker 进程（Phase 4）。

启动方式：
    python -m app.services.worker

职责：
- 消费 queue:incoming：对每条入站消息执行完整业务处理（路由 + 回复 + 日志 + 状态回写）
- 消费 queue:send_retry（低优先级）：处理出站发送失败的重试/退避
- Worker 自写心跳 worker:heartbeat:{pid}，TTL 120s
- 启动自检：把 wecom_inbound_event 中 status=processing 的僵尸消息重新入队
- 同一 userid 消息串行处理（Redis 分布式锁）
- 单条消息异常不影响进程存活；重试 2 次仍失败 → 死信

严格对齐：
- phase4-main.md §3.1 模块 B/E/G
- phase4-dev-implementation.md §4.2、§4.8
"""
from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.core.redis_client import (
    QUEUE_DEAD_LETTER,
    QUEUE_INCOMING,
    QUEUE_RATE_LIMIT_NOTIFY,
    QUEUE_SEND_RETRY,
    enqueue_message,
    get_redis,
    user_lock,
)
from app.db import SessionLocal
from app.models import AuditLog, ConversationLog, WecomInboundEvent
from app.schemas.conversation import ReplyMessage
from app.services import message_router
from app.wecom.callback import WeComMessage
from app.wecom.client import WeComClient, WeComError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

BLPOP_TIMEOUT_SECONDS = 5        # 阻塞超时，便于检查退出信号和 send_retry 队列
HEARTBEAT_INTERVAL = 60
HEARTBEAT_TTL = 120
MAX_RETRY = 2
SEND_RETRY_BACKOFFS = [60, 120, 300]  # 秒，指数退避
MAX_SEND_RETRY = 3
CONVERSATION_LOG_TTL_DAYS = 30

DEAD_LETTER_REPLY = "系统繁忙，请稍后再试。"


# ===========================================================================
# Worker 主类
# ===========================================================================

class Worker:
    """企微消息异步处理 Worker。"""

    def __init__(self) -> None:
        self._running = True
        self._pid = os.getpid()
        self._heartbeat_thread: threading.Thread | None = None
        self._redis = get_redis()
        self._wecom_client = WeComClient()

    # -----------------------------------------------------------------------
    # 启停
    # -----------------------------------------------------------------------

    def start(self) -> None:
        logger.info("worker: starting pid=%d", self._pid)
        self._setup_signal_handlers()
        self._start_heartbeat()
        self._startup_recovery()
        self._main_loop()
        logger.info("worker: stopped pid=%d", self._pid)

    def _setup_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_shutdown)
        signal.signal(signal.SIGINT, self._handle_shutdown)

    def _handle_shutdown(self, signum, frame) -> None:
        logger.info("worker: received signal %d, shutting down gracefully", signum)
        self._running = False

    # -----------------------------------------------------------------------
    # 心跳
    # -----------------------------------------------------------------------

    def _start_heartbeat(self) -> None:
        def _hb() -> None:
            while self._running:
                try:
                    self._redis.set(
                        f"worker:heartbeat:{self._pid}", "1", ex=HEARTBEAT_TTL,
                    )
                except Exception:
                    logger.warning("worker: heartbeat write failed", exc_info=True)
                # 用 short sleep 组合，便于快速退出
                for _ in range(HEARTBEAT_INTERVAL):
                    if not self._running:
                        return
                    time.sleep(1)

        self._heartbeat_thread = threading.Thread(
            target=_hb, daemon=True, name="worker-heartbeat",
        )
        self._heartbeat_thread.start()

    # -----------------------------------------------------------------------
    # 启动自检
    # -----------------------------------------------------------------------

    def _startup_recovery(self) -> None:
        """将上次 crash 残留的 status=processing 记录重入队列。

        ⚠️ 当前 Phase 4 设计默认单 Worker 运行。如果未来横向扩容成 N 个 Worker，
        每个 Worker 启动时都会看到全部 processing 记录并尝试重入队列，可能导致
        同一条消息被多个 Worker 重复入队（但最终由 user_lock + conversation_log
        UNIQUE 兜底，行为是幂等的）。若后续确认要多 Worker，再引入 owner_worker_id
        字段按归属过滤。
        """
        db = SessionLocal()
        try:
            rows = db.query(WecomInboundEvent).filter(
                WecomInboundEvent.status == "processing",
            ).all()
            if not rows:
                return
            for row in rows:
                queue_msg = _inbound_event_to_queue_msg(row)
                try:
                    enqueue_message(
                        json.dumps(queue_msg, ensure_ascii=False), QUEUE_INCOMING,
                    )
                    row.status = "received"
                    row.worker_started_at = None
                    logger.warning(
                        "worker: startup recovery requeue msg_id=%s", row.msg_id,
                    )
                except Exception:
                    logger.exception(
                        "worker: startup recovery failed msg_id=%s", row.msg_id,
                    )
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("worker: startup_recovery scan failed")
        finally:
            db.close()

    # -----------------------------------------------------------------------
    # 主循环
    # -----------------------------------------------------------------------

    def _main_loop(self) -> None:
        while self._running:
            try:
                item = self._redis.blpop(QUEUE_INCOMING, timeout=BLPOP_TIMEOUT_SECONDS)
            except Exception:
                logger.exception("worker: BLPOP failed, sleeping 1s")
                time.sleep(1)
                continue

            if item is None:
                # 空闲：优先处理"即发即弃"的限流通知（best-effort，不进重试队列）
                # 再处理 send_retry（带指数退避的出站重试）
                self._process_rate_limit_notify_once()
                self._process_send_retry_once()
                continue

            # item = (queue_name, payload_json)
            try:
                msg_data = json.loads(item[1])
            except Exception:
                logger.exception("worker: bad queue payload: %r", item[1])
                continue

            self._process_message(msg_data)

    # -----------------------------------------------------------------------
    # 单条消息处理
    # -----------------------------------------------------------------------

    def _process_message(self, msg_data: dict) -> None:
        userid = msg_data.get("from_userid") or ""
        inbound_event_id = msg_data.get("inbound_event_id")
        retry_count = int(msg_data.get("_retry_count") or 0)

        if not userid:
            logger.warning("worker: msg_data without from_userid: %s", msg_data)
            return

        # 分布式锁：同一 userid 串行（blocking_timeout=5 秒）
        with user_lock(userid, timeout=5) as acquired:
            if not acquired:
                logger.info("worker: user_lock busy, requeue userid=%s", userid)
                try:
                    time.sleep(0.5)
                    enqueue_message(
                        json.dumps(msg_data, ensure_ascii=False), QUEUE_INCOMING,
                    )
                except Exception:
                    logger.exception("worker: requeue after lock fail failed")
                return

            self._process_locked(msg_data, inbound_event_id, retry_count, userid)

    def _process_locked(
        self,
        msg_data: dict,
        inbound_event_id: Any,
        retry_count: int,
        userid: str,
    ) -> None:
        db: Session = SessionLocal()
        try:
            # inbound_event → processing
            self._mark_event_processing(db, inbound_event_id)

            msg = _build_wecom_message(msg_data)

            # 图片：Worker 层下载存 storage 并回填 image_url
            if msg.msg_type == "image" and msg.media_id:
                self._download_and_attach_image(msg)

            # 调路由
            replies = message_router.process(msg, db)

            # 提交 router 写入的 DB 改动（user / upload / delete 等）
            db.commit()

            # 发送回复（失败走 send_retry，不回滚整单）
            sent_ok = self._send_replies(replies)

            # 写 conversation_log
            self._write_conversation_log(db, msg, replies)
            db.commit()

            # inbound_event → done
            self._mark_event_done(db, inbound_event_id)
            db.commit()
            logger.info(
                "worker: processed msg_id=%s userid=%s replies=%d send_ok=%s",
                msg.msg_id, userid, len(replies), sent_ok,
            )
        except Exception as exc:
            db.rollback()
            logger.exception("worker: processing failed userid=%s: %s", userid, exc)
            self._handle_error(msg_data, inbound_event_id, retry_count, exc)
        finally:
            db.close()

    # -----------------------------------------------------------------------
    # 图片下载并附加到消息对象
    # -----------------------------------------------------------------------

    def _download_and_attach_image(self, msg: WeComMessage) -> None:
        try:
            from app.storage import get_storage

            blob = self._wecom_client.download_media(msg.media_id)
            storage = get_storage()
            key = f"images/{msg.from_user}/{msg.msg_id}.jpg"
            url = storage.save(key, blob, content_type="image/jpeg")
            msg.image_url = url
        except Exception:
            logger.exception(
                "worker: image download/save failed media_id=%s msg_id=%s",
                msg.media_id, msg.msg_id,
            )
            msg.image_url = ""

    # -----------------------------------------------------------------------
    # 回复发送（失败补偿）
    # -----------------------------------------------------------------------

    def _send_replies(self, replies: list[ReplyMessage]) -> bool:
        all_ok = True
        for reply in replies:
            ok = self._send_one(reply)
            if not ok:
                all_ok = False
        return all_ok

    def _send_one(self, reply: ReplyMessage) -> bool:
        try:
            self._wecom_client.send_text(reply.userid, reply.content)
            return True
        except WeComError as exc:
            return self._handle_send_error(reply, exc)
        except Exception as exc:
            logger.exception("worker: send unexpected error: %s", exc)
            self._enqueue_send_retry(reply, backoff=60)
            return False

    def _handle_send_error(self, reply: ReplyMessage, exc: WeComError) -> bool:
        errcode = getattr(exc, "errcode", 0)
        # token 过期：通过公开方法失效缓存后立即重试一次
        if errcode == 42001:
            try:
                self._wecom_client.invalidate_token()
                self._wecom_client.send_text(reply.userid, reply.content)
                return True
            except Exception:
                logger.exception("worker: retry after token refresh failed")
                self._enqueue_send_retry(reply, backoff=60)
                return False

        # 用户不存在或已退出：不重试
        if errcode in (60111, 84061, 40031):
            logger.warning(
                "worker: recipient unreachable userid=%s errcode=%s",
                reply.userid, errcode,
            )
            self._mark_user_inactive(reply.userid)
            return False

        # API 限流：入 send_retry
        if errcode in (45009, 45018):
            self._enqueue_send_retry(reply, backoff=60)
            return False

        # 其它错误：入 send_retry
        logger.warning(
            "worker: send_text failed, enqueue retry userid=%s errcode=%s msg=%s",
            reply.userid, errcode, exc,
        )
        self._enqueue_send_retry(reply, backoff=60)
        return False

    def _enqueue_send_retry(self, reply: ReplyMessage, backoff: int) -> None:
        payload = {
            "userid": reply.userid,
            "content": reply.content,
            "send_retry_count": 0,
            "backoff_until": time.time() + backoff,
        }
        try:
            self._redis.rpush(
                QUEUE_SEND_RETRY, json.dumps(payload, ensure_ascii=False),
            )
        except Exception:
            logger.exception("worker: enqueue send_retry failed")

    def _mark_user_inactive(self, userid: str) -> None:
        """企微返回 60111/84061/40031 时把用户标记为暂不可达。

        注意：user.status 枚举只有 active/blocked/deleted，没有 inactive。
        为避免把误报的用户永久封禁，这里只在 user.extra 里打标，保留 status=active。
        运营侧可据 `extra.wecom_unreachable=True` + last_active_at 做清理决策。
        """
        db = SessionLocal()
        try:
            from app.models import User
            user = db.query(User).filter(User.external_userid == userid).first()
            if user is None:
                return
            extra = dict(user.extra) if user.extra else {}
            extra["wecom_unreachable"] = True
            extra["wecom_unreachable_at"] = datetime.now(timezone.utc).isoformat()
            user.extra = extra
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("worker: mark_user_inactive failed userid=%s", userid)
        finally:
            db.close()

    # -----------------------------------------------------------------------
    # 限流通知队列消费（best-effort，即发即弃）
    # -----------------------------------------------------------------------

    def _process_rate_limit_notify_once(self) -> None:
        """消费 queue:rate_limit_notify。

        设计约束（对齐 webhook 端的 push 策略）：
        - 发失败 → 直接丢弃，不重试、不入 send_retry（限流提示本身不是关键数据）
        - 保持 idempotent：webhook 侧 60s 内同一用户不会重复 push，所以发一次即可
        - 与 send_retry 隔离：防止限流风暴挤占真正的业务 retry
        """
        try:
            raw = self._redis.lpop(QUEUE_RATE_LIMIT_NOTIFY)
        except Exception:
            logger.exception("worker: lpop rate_limit_notify failed")
            return
        if not raw:
            return

        try:
            payload = json.loads(raw)
        except Exception:
            logger.exception("worker: bad rate_limit_notify payload: %r", raw)
            return

        userid = payload.get("userid") or ""
        content = payload.get("content") or ""
        if not userid or not content:
            return

        try:
            self._wecom_client.send_text(userid, content)
        except Exception as exc:
            # 不重试：限流场景下失败再重试只会雪崩
            logger.warning(
                "worker: rate_limit_notify send failed (drop) userid=%s err=%s",
                userid, exc,
            )

    # -----------------------------------------------------------------------
    # send_retry 队列消费（低优先级）
    # -----------------------------------------------------------------------

    def _process_send_retry_once(self) -> None:
        try:
            raw = self._redis.lpop(QUEUE_SEND_RETRY)
        except Exception:
            logger.exception("worker: lpop send_retry failed")
            return
        if not raw:
            return

        try:
            payload = json.loads(raw)
        except Exception:
            logger.exception("worker: bad send_retry payload: %r", raw)
            return

        backoff_until = float(payload.get("backoff_until") or 0)
        now = time.time()
        if now < backoff_until:
            # 未到退避时间，放回队尾
            try:
                self._redis.rpush(QUEUE_SEND_RETRY, json.dumps(payload, ensure_ascii=False))
            except Exception:
                logger.exception("worker: requeue backoff msg failed")
            # 小睡一会防止空跑
            time.sleep(0.5)
            return

        userid = payload.get("userid") or ""
        content = payload.get("content") or ""
        retry_count = int(payload.get("send_retry_count") or 0)

        try:
            self._wecom_client.send_text(userid, content)
            logger.info("worker: send_retry success userid=%s retries=%d", userid, retry_count)
            return
        except WeComError as exc:
            errcode = getattr(exc, "errcode", 0)
            if errcode in (60111, 84061, 40031):
                self._mark_user_inactive(userid)
                return
        except Exception as exc:
            logger.warning("worker: send_retry network err: %s", exc)

        # 仍失败 → 指数退避 or 放弃
        if retry_count + 1 >= MAX_SEND_RETRY:
            self._write_send_failed_audit(userid, content, retry_count + 1)
            return

        next_backoff = SEND_RETRY_BACKOFFS[min(retry_count, len(SEND_RETRY_BACKOFFS) - 1)]
        payload["send_retry_count"] = retry_count + 1
        payload["backoff_until"] = time.time() + next_backoff
        try:
            self._redis.rpush(QUEUE_SEND_RETRY, json.dumps(payload, ensure_ascii=False))
        except Exception:
            logger.exception("worker: requeue with backoff failed")

    def _write_send_failed_audit(self, userid: str, content: str, retries: int) -> None:
        db = SessionLocal()
        try:
            db.add(AuditLog(
                target_type="user",
                target_id=userid,
                action="auto_reject",
                reason=f"wecom_send_failed after {retries} retries",
                operator="worker",
                snapshot={"content_preview": content[:200]},
            ))
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("worker: write send_failed audit_log failed")
        finally:
            db.close()

    # -----------------------------------------------------------------------
    # inbound_event 状态回写
    # -----------------------------------------------------------------------

    def _mark_event_processing(self, db: Session, event_id: Any) -> None:
        if not event_id:
            return
        try:
            db.query(WecomInboundEvent).filter(
                WecomInboundEvent.id == event_id,
            ).update({
                "status": "processing",
                "worker_started_at": datetime.now(timezone.utc),
            })
        except Exception:
            logger.exception("worker: mark_event_processing failed id=%s", event_id)

    def _mark_event_done(self, db: Session, event_id: Any) -> None:
        if not event_id:
            return
        try:
            db.query(WecomInboundEvent).filter(
                WecomInboundEvent.id == event_id,
            ).update({
                "status": "done",
                "worker_finished_at": datetime.now(timezone.utc),
            })
        except Exception:
            logger.exception("worker: mark_event_done failed id=%s", event_id)

    def _mark_event_fail(
        self, event_id: Any, new_status: str, error_msg: str, retry_count: int,
    ) -> None:
        if not event_id:
            return
        db = SessionLocal()
        try:
            db.query(WecomInboundEvent).filter(
                WecomInboundEvent.id == event_id,
            ).update({
                "status": new_status,
                "error_message": error_msg[:1000],
                "retry_count": retry_count,
                "worker_finished_at": datetime.now(timezone.utc),
            })
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("worker: mark_event_fail failed id=%s", event_id)
        finally:
            db.close()

    def _update_retry_and_error_keep_processing(
        self, event_id: Any, retry_count: int, error_msg: str,
    ) -> None:
        """入队失败时仅更新 retry_count / error，保持 status=processing。

        这样 _startup_recovery 下次启动会捕获这条记录并重新入队，避免数据丢失
        （对比 P0-1：若直接置 failed 后入队又失败，此记录再也不会被消费）。
        """
        if not event_id:
            return
        db = SessionLocal()
        try:
            db.query(WecomInboundEvent).filter(
                WecomInboundEvent.id == event_id,
            ).update({
                "error_message": error_msg[:1000],
                "retry_count": retry_count,
            })
            db.commit()
        except Exception:
            db.rollback()
            logger.exception(
                "worker: update_retry_and_error failed id=%s", event_id,
            )
        finally:
            db.close()

    # -----------------------------------------------------------------------
    # conversation_log
    # -----------------------------------------------------------------------

    def _write_conversation_log(
        self, db: Session, msg: WeComMessage, replies: list[ReplyMessage],
    ) -> None:
        """入站 + 出站对话日志写入。

        入站消息的 wecom_msg_id 必须唯一；若 startup_recovery 或手动重投后
        重复写入会触发 UNIQUE 冲突。策略：
        - 先尝试正常 add + flush；
        - 冲突时 rollback 该行（不影响整单），并用已存在的记录判定幂等。
        """
        from datetime import timedelta
        from sqlalchemy.exc import IntegrityError

        now = datetime.now(timezone.utc)
        expires = now + timedelta(days=CONVERSATION_LOG_TTL_DAYS)

        log_msg_type = _coerce_log_msg_type(msg.msg_type)
        inbound_content = msg.content or msg.media_id or ""

        # 入站：wecom_msg_id UNIQUE，需独立子事务保护
        try:
            with db.begin_nested():
                db.add(ConversationLog(
                    userid=msg.from_user,
                    direction="in",
                    msg_type=log_msg_type,
                    content=inbound_content,
                    wecom_msg_id=msg.msg_id,
                    expires_at=expires,
                ))
        except IntegrityError:
            # 典型场景：重投或自检恢复，已有同 wecom_msg_id 记录
            logger.info(
                "worker: inbound conversation_log UNIQUE hit, skip msg_id=%s",
                msg.msg_id,
            )
        except Exception:
            logger.exception("worker: write inbound conversation_log failed")

        # 出站：wecom_msg_id 必须 NULL，避免 UNIQUE 冲突
        for reply in replies:
            try:
                with db.begin_nested():
                    db.add(ConversationLog(
                        userid=reply.userid,
                        direction="out",
                        msg_type="text",
                        content=reply.content,
                        wecom_msg_id=None,
                        intent=reply.intent,
                        criteria_snapshot=reply.criteria_snapshot,
                        expires_at=expires,
                    ))
            except Exception:
                logger.exception("worker: write outbound conversation_log failed")

    # -----------------------------------------------------------------------
    # 错误处理
    # -----------------------------------------------------------------------

    def _handle_error(
        self,
        msg_data: dict,
        event_id: Any,
        retry_count: int,
        error: Exception,
    ) -> None:
        error_text = f"{type(error).__name__}: {error}"
        new_retry = retry_count + 1

        if retry_count < MAX_RETRY:
            # 准备重入队列
            msg_data["_retry_count"] = new_retry
            try:
                enqueue_message(
                    json.dumps(msg_data, ensure_ascii=False), QUEUE_INCOMING,
                )
            except Exception:
                # P0-1：入队失败 → 保持 status=processing，仅累加 retry_count
                # _startup_recovery 会扫 status=processing 并重新入队，避免消息丢失
                logger.exception(
                    "worker: requeue on retry failed, keep status=processing "
                    "for startup_recovery to catch up"
                )
                self._update_retry_and_error_keep_processing(
                    event_id, new_retry, error_text,
                )
                return

            # 入队成功 → 标 failed（等待下一轮消费）
            self._mark_event_fail(event_id, "failed", error_text, new_retry)
            return

        # 达到 MAX_RETRY → 死信
        try:
            enqueue_message(
                json.dumps(msg_data, ensure_ascii=False), QUEUE_DEAD_LETTER,
            )
        except Exception:
            # 死信入队失败仍应落库 dead_letter，Worker 不再自动恢复
            # （运营侧从 status=dead_letter + error_message 介入）
            logger.exception("worker: push to dead_letter failed")
        self._mark_event_fail(event_id, "dead_letter", error_text, new_retry)

        # 兜底回复
        try:
            self._wecom_client.send_text(msg_data.get("from_userid", ""), DEAD_LETTER_REPLY)
        except Exception:
            logger.warning("worker: dead-letter fallback reply failed", exc_info=True)


# ===========================================================================
# 工具
# ===========================================================================

def _build_wecom_message(msg_data: dict) -> WeComMessage:
    return WeComMessage(
        msg_id=msg_data.get("msg_id") or "",
        from_user=msg_data.get("from_userid") or "",
        to_user="",
        msg_type=msg_data.get("msg_type") or "",
        content=msg_data.get("content") or "",
        media_id=msg_data.get("media_id") or "",
        create_time=int(msg_data.get("create_time") or 0),
    )


def _inbound_event_to_queue_msg(row: WecomInboundEvent) -> dict:
    """把 wecom_inbound_event 行重建成入队消息 JSON。

    P0-2 修复要点：
    - msg_type 直接使用 row.msg_type（DB 枚举已覆盖全部 WeCom 类型）
    - media_id 来自独立列（image/voice/video/file 类型，Worker 恢复时可补下载）
    - content 只对 text/event 类型有意义；媒体类型 content_brief 只是类型标签，
      不要把它当 text content 传给 router（否则 message_router 会当成用户在发字面文本）
    """
    raw_type = row.msg_type or "text"
    content = row.content_brief or ""
    if raw_type in ("image", "voice", "video", "file"):
        # 媒体消息：content_brief 是 "[image] media_id saved" 之类占位，
        # 业务链路不应该把它当正文
        content = ""

    return {
        "msg_id": row.msg_id,
        "from_userid": row.from_userid,
        "msg_type": raw_type,
        "content": content,
        "media_id": row.media_id or "",
        "create_time": int(row.created_at.timestamp()) if row.created_at else 0,
        "inbound_event_id": row.id,
        "_retry_count": int(row.retry_count or 0),
        "_recovered": True,
    }


_LOG_MSG_TYPE_MAP = {
    "text": "text",
    "image": "image",
    "voice": "voice",
    "event": "system",
    "file": "system",
    "video": "system",
    "link": "system",
    "location": "system",
}


def _coerce_log_msg_type(mtype: str) -> str:
    return _LOG_MSG_TYPE_MAP.get(mtype or "", "system")


# ===========================================================================
# CLI 入口
# ===========================================================================

def main() -> None:
    # 基础日志配置（如果启动时未配置 logger）
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        )
    Worker().start()


if __name__ == "__main__":
    main()
