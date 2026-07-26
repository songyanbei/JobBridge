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

from sqlalchemy import and_, exists, func, null, or_, text
from sqlalchemy.orm import Session, aliased

from app.core.redis_client import (
    QUEUE_DEAD_LETTER,
    QUEUE_INCOMING,
    QUEUE_RATE_LIMIT_NOTIFY,
    QUEUE_SEND_RETRY,
    enqueue_message,
    get_redis,
    user_lock,
)
from app.core.logging_setup import configure_loguru, identifier_hash
from app.db import SessionLocal
from app.models import (
    AuditLog,
    ConversationLog,
    WecomInboundEvent,
    WecomOutboundOutbox,
    RecommendationDelivery,
)
from app.schemas.conversation import ReplyMessage
from app.services import conversation_service, message_router, search_service
from app.tasks.common import log_event
from app.wecom.callback import WeComMessage
from app.wecom.client import WeComClient, WeComError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

BLPOP_TIMEOUT_SECONDS = 1        # 小于 Redis socket timeout，便于检查退出/辅助队列
HEARTBEAT_INTERVAL = 60
HEARTBEAT_TTL = 120
MAX_RETRY = 2
SEND_RETRY_BACKOFFS = [60, 120, 300]  # 秒，指数退避
MAX_SEND_RETRY = 3
CONVERSATION_LOG_TTL_DAYS = 30
AUX_QUEUE_EVERY_MESSAGES = 10
RECOVERY_SCAN_INTERVAL_SECONDS = 30
RECOVERY_RECEIVED_STALE_SECONDS = 10
RECOVERY_PROCESSING_STALE_SECONDS = 180
OUTBOX_SENDING_STALE_SECONDS = 180
OUTBOX_CLAIM_BATCH_SIZE = 20
OUTBOX_MAX_ATTEMPTS = 5
OUTBOX_RETRY_BACKOFFS = [30, 60, 120, 300, 600]
SESSION_COMMIT_STALE_SECONDS = 180
SESSION_COMMIT_RETRY_BACKOFFS = [5, 15, 30, 60, 300]

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
        self._last_recovery_scan = 0.0

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
        """Recover stale durable events without stealing active work."""
        db = SessionLocal()
        try:
            # Use the database clock for every durable timestamp comparison.
            # Containers commonly run UTC while MySQL is configured with a local
            # timezone; mixing those clocks can recover active work eight hours
            # early (or leave stale work untouched for eight hours).
            received_before = func.timestampadd(
                text("SECOND"), -RECOVERY_RECEIVED_STALE_SECONDS, func.now(6),
            )
            processing_before = func.timestampadd(
                text("SECOND"), -RECOVERY_PROCESSING_STALE_SECONDS, func.now(6),
            )
            rows = (
                db.query(WecomInboundEvent)
                .filter(or_(
                    and_(
                        WecomInboundEvent.status == "received",
                        WecomInboundEvent.created_at <= received_before,
                    ),
                    and_(
                        WecomInboundEvent.status == "processing",
                        or_(
                            WecomInboundEvent.worker_started_at.is_(None),
                            WecomInboundEvent.worker_started_at <= processing_before,
                        ),
                    ),
                    # A retry is marked failed only after its replacement queue
                    # item has been written. If a worker then crashes after BLPOP
                    # but before changing it back to processing, that item would
                    # otherwise be lost forever.
                    and_(
                        WecomInboundEvent.status == "failed",
                        or_(
                            WecomInboundEvent.worker_finished_at.is_(None),
                            WecomInboundEvent.worker_finished_at <= processing_before,
                        ),
                    ),
                ))
                .with_for_update(skip_locked=True)
                .limit(100)
                .all()
            )
            if not rows:
                db.commit()
                self._last_recovery_scan = time.monotonic()
                return
            claimed: list[tuple[str, dict]] = []
            for row in rows:
                claimed.append((row.msg_id, _inbound_event_to_queue_msg(row)))
                # Commit the durable claim before touching Redis. If the process
                # dies after this commit, the processing-stale branch recovers it
                # later. The inverse order can enqueue the same 100 rows on every
                # failed DB commit and create an unbounded queue storm.
                row.status = "processing"
                row.worker_started_at = func.now(6)
            db.commit()

            for msg_id, queue_msg in claimed:
                try:
                    enqueue_message(
                        json.dumps(queue_msg, ensure_ascii=False), QUEUE_INCOMING,
                    )
                    logger.warning(
                        "worker: startup recovery requeue msg_id=%s", msg_id,
                    )
                except Exception:
                    logger.exception(
                        "worker: startup recovery failed msg_id=%s", msg_id,
                    )
                    self._mark_event_recovery_due(
                        queue_msg.get("inbound_event_id"),
                        error_message="startup recovery enqueue failed",
                    )
        except Exception:
            db.rollback()
            logger.exception("worker: startup_recovery scan failed")
        finally:
            # A DB/Redis outage must not turn the main loop into a hot recovery
            # retry loop. The next bounded scan runs after the normal interval.
            self._last_recovery_scan = time.monotonic()
            db.close()

    # -----------------------------------------------------------------------
    # 主循环
    # -----------------------------------------------------------------------

    def _main_loop(self) -> None:
        processed_since_aux = 0
        while self._running:
            if (
                time.monotonic() - self._last_recovery_scan
                >= RECOVERY_SCAN_INTERVAL_SECONDS
            ):
                self._startup_recovery()
            try:
                item = self._redis.blpop(QUEUE_INCOMING, timeout=BLPOP_TIMEOUT_SECONDS)
            except Exception:
                logger.exception("worker: BLPOP failed, sleeping 1s")
                time.sleep(1)
                continue

            if item is None:
                # 空闲：优先处理"即发即弃"的限流通知（best-effort，不进重试队列）
                # 再处理 send_retry（带指数退避的出站重试）
                self._service_aux_queues()
                processed_since_aux = 0
                continue

            # item = (queue_name, payload_json)
            try:
                msg_data = json.loads(item[1])
            except Exception:
                logger.exception("worker: bad queue payload: %r", item[1])
                continue

            self._process_message(msg_data)
            processed_since_aux += 1
            if processed_since_aux >= AUX_QUEUE_EVERY_MESSAGES:
                # 持续 inbound 流量下也给通知/发送重试固定时间片，避免只有队列
                # 完全清空时才消费而永久饥饿。
                self._service_aux_queues()
                processed_since_aux = 0

    def _service_aux_queues(self) -> None:
        self._process_rate_limit_notify_once()
        self._process_session_commit_once()
        self._process_outbox_once()
        self._process_send_retry_once()

    # -----------------------------------------------------------------------
    # 单条消息处理
    # -----------------------------------------------------------------------

    def _process_message(self, msg_data: dict) -> None:
        process_started = time.perf_counter()
        userid = msg_data.get("from_userid") or ""
        inbound_event_id = msg_data.get("inbound_event_id")
        retry_count = int(msg_data.get("_retry_count") or 0)

        if not userid:
            logger.warning(
                "worker: queue payload without from_userid msg_id=%s keys=%s",
                msg_data.get("msg_id"),
                sorted(msg_data),
            )
            return

        try:
            enqueued_at = float(msg_data.get("_enqueued_at") or 0)
        except (TypeError, ValueError):
            enqueued_at = 0
        queue_wait_ms = (
            max(0, int((time.time() - enqueued_at) * 1000))
            if enqueued_at > 0 else None
        )
        user_hash = identifier_hash(userid)
        lock_started = time.perf_counter()
        outcome = "unknown"

        # 分布式锁：同一 userid 串行（blocking_timeout=5 秒）
        try:
            with user_lock(userid, timeout=5) as lock_lease:
                lock_wait_ms = int((time.perf_counter() - lock_started) * 1000)
                if not lock_lease:
                    outcome = "lock_busy_requeued"
                    logger.info(
                        "worker: user_lock busy, requeue user_hash=%s", user_hash,
                    )
                    try:
                        time.sleep(0.5)
                        enqueue_message(
                            json.dumps(msg_data, ensure_ascii=False), QUEUE_INCOMING,
                        )
                    except Exception:
                        outcome = "lock_busy_requeue_failed"
                        logger.exception("worker: requeue after lock fail failed")
                        self._preserve_event_for_recovery(inbound_event_id)
                    return

                # Redis lock guarantees“同一时刻只有一个”，但不保证等待者按入队
                # 顺序获得锁。以 durable inbound_event.id 作单调序列门禁，后到消息
                # 若发现同用户仍有更早未完成事件，只重入队而不触碰 session/DB。
                if self._has_earlier_unfinished_event(userid, inbound_event_id):
                    outcome = "out_of_order_requeued"
                    try:
                        enqueue_message(
                            json.dumps(msg_data, ensure_ascii=False), QUEUE_INCOMING,
                        )
                    except Exception:
                        outcome = "out_of_order_requeue_failed"
                        logger.exception(
                            "worker: ordered requeue failed msg_id=%s",
                            msg_data.get("msg_id"),
                        )
                        self._preserve_event_for_recovery(inbound_event_id)
                    return

                outcome = self._process_locked(
                    msg_data, inbound_event_id, retry_count, userid, lock_lease,
                )
        finally:
            log_event(
                "message_processing",
                msg_id=msg_data.get("msg_id"),
                user_hash=user_hash,
                queue_wait_ms=queue_wait_ms,
                lock_wait_ms=int((time.perf_counter() - lock_started) * 1000)
                if "lock_wait_ms" not in locals() else lock_wait_ms,
                process_duration_ms=int((time.perf_counter() - process_started) * 1000),
                retry_count=retry_count,
                outcome=outcome,
            )

    def _has_earlier_unfinished_event(
        self, userid: str, inbound_event_id: Any,
    ) -> bool:
        """Whether a durable earlier message for this user still owns sequence priority."""
        if not inbound_event_id:
            return False
        db = SessionLocal()
        try:
            return db.query(WecomInboundEvent.id).filter(
                WecomInboundEvent.from_userid == userid,
                WecomInboundEvent.id < int(inbound_event_id),
                WecomInboundEvent.status.in_(
                    ("received", "processing", "session_pending", "failed"),
                ),
            ).first() is not None
        except Exception:
            # Availability must not silently defeat ordering. Requeue and retry the
            # read rather than process a possibly stale turn.
            logger.exception(
                "worker: earlier-event order check failed msg_id=%s",
                inbound_event_id,
            )
            return True
        finally:
            db.close()

    def _preserve_event_for_recovery(self, inbound_event_id: Any) -> None:
        """Make a popped-but-not-requeued event visible to startup recovery."""
        self._mark_event_recovery_due(inbound_event_id)

    def _process_locked(
        self,
        msg_data: dict,
        inbound_event_id: Any,
        retry_count: int,
        userid: str,
        lock_lease: Any = None,
    ) -> str:
        db: Session = SessionLocal()
        business_committed = False
        try:
            # Redis is an at-least-once queue. A crash between enqueue and the DB
            # claim can leave a duplicate payload behind. The per-user lease
            # serializes contenders, and this durable terminal check prevents the
            # later copy from repeating publish/search side effects.
            if inbound_event_id:
                event_status = db.query(WecomInboundEvent.status).filter(
                    WecomInboundEvent.id == inbound_event_id,
                ).scalar()
                if event_status in ("done", "dead_letter"):
                    logger.info(
                        "worker: terminal duplicate skipped id=%s status=%s",
                        inbound_event_id,
                        event_status,
                    )
                    return "duplicate_terminal_skipped"
                if event_status == "session_pending":
                    applied = self._apply_session_commit_for_event(
                        inbound_event_id, userid,
                    )
                    if applied:
                        self._deliver_outbox_for_event(inbound_event_id)
                        return "session_pending_recovered"
                    return "session_pending_waiting"

            # inbound_event → processing
            self._mark_event_processing(db, inbound_event_id)
            # 独立提交处理起点；若和 router 的慢 LLM 事务一起提交，数据库看到的
            # worker_started_at 会接近“处理完成”，queue/process 两项指标失真。
            db.commit()

            msg = _build_wecom_message(msg_data)

            # 图片：Worker 层下载存 storage 并回填 image_url
            if msg.msg_type == "image" and msg.media_id:
                self._download_and_attach_image(msg)

            # 调路由；把 pop 后的队列深度作为 turn-scoped hint 传给 reranker。
            # ContextVar 隔离 future thread/task，避免每次排序再额外访问 Redis。
            try:
                backlog_depth = int(self._redis.llen(QUEUE_INCOMING) or 0)
            except Exception:
                backlog_depth = 0
            backlog_token = search_service.set_queue_backlog_hint(backlog_depth)
            stage_token = conversation_service.begin_session_staging(userid)
            staged_session = None
            try:
                replies = message_router.process(msg, db)
            finally:
                staged_session = conversation_service.end_session_staging(
                    stage_token,
                )
                search_service.reset_queue_backlog_hint(backlog_token)

            # Router 只暂存了 session 意图；DB 提交前再次验证租约。续租线程一旦
            # 发现 extend 失败会置 lost_event，使旧 Worker 走 rollback + 可恢复重试。
            if hasattr(lock_lease, "assert_owned"):
                lock_lease.assert_owned()

            # 业务写入、对话日志、回复意图和 inbound done 必须在同一个事务提交。
            # 提交后即使进程崩溃，terminal gate 不会重跑 router；未发送回复由
            # durable outbox 扫描恢复。
            self._write_conversation_log(db, msg, replies)
            if inbound_event_id:
                self._stage_outbox(db, inbound_event_id, replies)
            if inbound_event_id and staged_session is not None:
                self._stage_session_commit(
                    db, inbound_event_id, staged_session,
                )
            else:
                self._mark_event_done(db, inbound_event_id)
            db.commit()
            business_committed = True

            # Session 必须先落 Redis 并把 durable event 标 done，outbox 才可见。
            # Redis 短断时业务事务已经安全提交，后续 turn 被 session_pending 顺序
            # 门禁阻塞，由辅助扫描恢复，不会重跑发布/搜索。
            session_ready = True
            if inbound_event_id and staged_session is not None:
                session_ready = self._apply_session_commit_for_event(
                    inbound_event_id, userid,
                )
            elif not inbound_event_id and staged_session is not None:
                session_ready = conversation_service.apply_staged_session(
                    staged_session,
                )
            sent_ok = False
            if session_ready:
                sent_ok = (
                    self._deliver_outbox_for_event(inbound_event_id)
                    if inbound_event_id
                    else self._send_replies(replies)
                )
            logger.info(
                "worker: processed msg_id=%s user_hash=%s replies=%d send_ok=%s",
                msg.msg_id, identifier_hash(userid), len(replies), sent_ok,
            )
            return "processed" if session_ready else "session_commit_pending"
        except Exception as exc:
            db.rollback()
            if business_committed:
                # Never send an already-committed publish/search turn through the
                # generic retry path. Its durable session/outbox state is the sole
                # recovery source; replaying the router could duplicate business
                # side effects.
                logger.exception(
                    "worker: post-commit recovery failed user_hash=%s: %s",
                    identifier_hash(userid),
                    exc,
                )
                return "post_commit_recovery_pending"
            logger.exception(
                "worker: processing failed user_hash=%s: %s",
                identifier_hash(userid),
                exc,
            )
            self._handle_error(msg_data, inbound_event_id, retry_count, exc)
            return "processing_failed"
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

    def _stage_session_commit(
        self,
        db: Session,
        inbound_event_id: Any,
        commit: conversation_service.StagedSessionCommit,
    ) -> None:
        updated = db.query(WecomInboundEvent).filter(
            WecomInboundEvent.id == inbound_event_id,
        ).update({
            "status": "session_pending",
            "session_operation": commit.operation,
            "session_expected_version": commit.expected_version,
            "session_payload": commit.payload,
            "session_apply_attempts": 0,
            "session_apply_locked_at": None,
            "session_next_attempt_at": None,
            "session_applied_at": None,
            "worker_finished_at": None,
        })
        if updated != 1:
            raise RuntimeError(
                f"unable to stage session commit for event {inbound_event_id}",
            )

    def _claim_session_commits(
        self,
        *,
        inbound_event_id: Any = None,
        limit: int = 10,
    ) -> list[dict]:
        db = SessionLocal()
        try:
            now = func.now(6)
            stale_before = func.timestampadd(
                text("SECOND"), -SESSION_COMMIT_STALE_SECONDS, now,
            )
            query = db.query(WecomInboundEvent).filter(
                WecomInboundEvent.status == "session_pending",
                or_(
                    WecomInboundEvent.session_next_attempt_at.is_(None),
                    WecomInboundEvent.session_next_attempt_at <= now,
                ),
                or_(
                    WecomInboundEvent.session_apply_locked_at.is_(None),
                    WecomInboundEvent.session_apply_locked_at <= stale_before,
                ),
            )
            if inbound_event_id:
                query = query.filter(
                    WecomInboundEvent.id == int(inbound_event_id),
                )
            rows = (
                query.order_by(WecomInboundEvent.id)
                .with_for_update(skip_locked=True)
                .limit(limit)
                .all()
            )
            claimed: list[dict] = []
            for row in rows:
                row.session_apply_locked_at = now
                row.session_apply_attempts = int(
                    row.session_apply_attempts or 0,
                ) + 1
                claimed.append({
                    "event_id": int(row.id),
                    "userid": row.from_userid,
                    "attempts": int(row.session_apply_attempts),
                    "commit": conversation_service.StagedSessionCommit(
                        userid=row.from_userid,
                        operation=str(row.session_operation or ""),
                        expected_version=int(
                            row.session_expected_version or 0,
                        ),
                        payload=(
                            dict(row.session_payload)
                            if row.session_payload is not None
                            else None
                        ),
                    ),
                })
            db.commit()
            return claimed
        except Exception:
            db.rollback()
            logger.exception("worker: claim durable session commits failed")
            return []
        finally:
            db.close()

    def _mark_session_commit_applied(self, event_id: int) -> bool:
        db = SessionLocal()
        try:
            updated = db.query(WecomInboundEvent).filter(
                WecomInboundEvent.id == event_id,
                WecomInboundEvent.status == "session_pending",
            ).update({
                "status": "done",
                # The payload can contain transient search/draft PII. It is only
                # needed until Redis confirms the transition, so do not retain it
                # for the normal inbound-event TTL.
                "session_operation": None,
                "session_expected_version": None,
                "session_payload": null(),
                "session_apply_locked_at": None,
                "session_next_attempt_at": None,
                "session_applied_at": func.now(6),
                "worker_finished_at": func.now(6),
                "error_message": None,
            })
            if updated == 1:
                delivery_ids = db.query(
                    WecomOutboundOutbox.recommendation_delivery_id,
                ).filter(
                    WecomOutboundOutbox.inbound_event_id == event_id,
                    WecomOutboundOutbox.recommendation_delivery_id.isnot(None),
                )
                db.query(RecommendationDelivery).filter(
                    RecommendationDelivery.delivery_id.in_(delivery_ids),
                    RecommendationDelivery.status == "prepared",
                ).update({"status": "pending"}, synchronize_session=False)
            db.commit()
            return updated == 1
        except Exception:
            db.rollback()
            logger.exception(
                "worker: mark durable session commit applied failed event_id=%s",
                event_id,
            )
            return False
        finally:
            db.close()

    def _mark_session_commit_retry(self, item: dict, error: Exception) -> None:
        db = SessionLocal()
        try:
            attempts = int(item["attempts"])
            backoff = SESSION_COMMIT_RETRY_BACKOFFS[
                min(attempts - 1, len(SESSION_COMMIT_RETRY_BACKOFFS) - 1)
            ]
            db.query(WecomInboundEvent).filter(
                WecomInboundEvent.id == item["event_id"],
                WecomInboundEvent.status == "session_pending",
            ).update({
                "session_apply_locked_at": None,
                "session_next_attempt_at": func.timestampadd(
                    text("SECOND"), backoff, func.now(6),
                ),
                "error_message": f"{type(error).__name__}: {error}"[:1000],
            })
            db.commit()
        except Exception:
            db.rollback()
            logger.exception(
                "worker: persist session commit retry failed event_id=%s",
                item.get("event_id"),
            )
        finally:
            db.close()

    def _apply_session_commit_item(self, item: dict) -> bool:
        commit = item["commit"]
        try:
            applied = conversation_service.apply_staged_session(commit)
            if not applied:
                applied = conversation_service.is_staged_session_applied(commit)
            if not applied:
                raise conversation_service.SessionVersionConflict(
                    "durable session CAS rejected",
                )
        except Exception as exc:
            self._mark_session_commit_retry(item, exc)
            return False
        return self._mark_session_commit_applied(item["event_id"])

    def _apply_session_commit_for_event(
        self,
        inbound_event_id: Any,
        userid: str,
    ) -> bool:
        claimed = self._claim_session_commits(
            inbound_event_id=inbound_event_id,
            limit=1,
        )
        if not claimed:
            # Another worker may have completed it between the status read and
            # claim. Read the durable terminal state instead of assuming failure.
            db = SessionLocal()
            try:
                status = db.query(WecomInboundEvent.status).filter(
                    WecomInboundEvent.id == inbound_event_id,
                ).scalar()
                return status == "done"
            finally:
                db.close()
        item = claimed[0]
        if item["userid"] != userid:
            self._mark_session_commit_retry(
                item, RuntimeError("session commit userid mismatch"),
            )
            return False
        return self._apply_session_commit_item(item)

    def _process_session_commit_once(self) -> None:
        for item in self._claim_session_commits():
            try:
                with user_lock(item["userid"], timeout=5) as lease:
                    if not lease:
                        self._mark_session_commit_retry(
                            item, RuntimeError("user lock busy"),
                        )
                        continue
                    lease.assert_owned()
                    self._apply_session_commit_item(item)
            except Exception as exc:
                self._mark_session_commit_retry(item, exc)

    def _stage_outbox(
        self,
        db: Session,
        inbound_event_id: Any,
        replies: list[ReplyMessage],
    ) -> None:
        """在 router 业务事务中持久化有序回复意图。"""
        for index, reply in enumerate(replies):
            if reply.recommendation_context:
                from app.services.recommendation_delivery_service import prepare_delivery
                ctx = reply.recommendation_context
                prepare_delivery(
                    db,
                    inbound_event_id=int(inbound_event_id),
                    reply_index=index,
                    userid=reply.userid,
                    body=reply.content,
                    request_id=ctx.request_id,
                    snapshot_id=ctx.snapshot_id,
                    position_count=len(ctx.items),
                    delivery_id=ctx.delivery_id,
                    recommendation_context=ctx.model_dump(mode="json"),
                    source_inbound_msg_id=(
                        reply.recommendation_request.source_inbound_msg_id
                        if reply.recommendation_request else str(inbound_event_id)
                    ),
                    request_fact=(
                        reply.recommendation_request.model_dump(mode="json")
                        if reply.recommendation_request else None
                    ),
                )
                continue
            db.add(WecomOutboundOutbox(
                inbound_event_id=int(inbound_event_id),
                reply_index=index,
                userid=reply.userid,
                msg_type=reply.msg_type,
                content=reply.content,
                intent=reply.intent,
                criteria_snapshot=reply.criteria_snapshot,
                status="pending",
            ))

    def _claim_outbox(
        self,
        *,
        inbound_event_id: Any = None,
        limit: int = OUTBOX_CLAIM_BATCH_SIZE,
    ) -> list[dict]:
        """原子认领到期 outbox；stale sending 在 Worker crash 后可恢复。"""
        db = SessionLocal()
        try:
            now = func.now(6)
            stale_before = func.timestampadd(
                text("SECOND"), -OUTBOX_SENDING_STALE_SECONDS, now,
            )
            ambiguous = db.query(WecomOutboundOutbox).filter(
                WecomOutboundOutbox.status == "sending",
                WecomOutboundOutbox.locked_at <= stale_before,
                WecomOutboundOutbox.recommendation_delivery_id.isnot(None),
            ).with_for_update(skip_locked=True).all()
            for stale in ambiguous:
                stale.status = "dead_letter"
                stale.locked_at = None
                stale.last_error = "ambiguous provider outcome; automatic resend disabled"
                delivery = db.get(
                    RecommendationDelivery, stale.recommendation_delivery_id,
                )
                if delivery and delivery.status == "sending":
                    delivery.status = "unknown"
                    delivery.last_error = stale.last_error
            due = or_(
                and_(
                    WecomOutboundOutbox.status == "pending",
                    or_(
                        WecomOutboundOutbox.next_attempt_at.is_(None),
                        WecomOutboundOutbox.next_attempt_at <= now,
                    ),
                ),
                and_(
                    WecomOutboundOutbox.status == "sending",
                    WecomOutboundOutbox.locked_at <= stale_before,
                    WecomOutboundOutbox.recommendation_delivery_id.is_(None),
                ),
            )
            earlier = aliased(WecomOutboundOutbox)
            no_earlier_unsent_for_user = ~exists().where(and_(
                earlier.userid == WecomOutboundOutbox.userid,
                earlier.id < WecomOutboundOutbox.id,
                earlier.status.in_(("pending", "sending")),
            ))
            query = db.query(WecomOutboundOutbox).join(
                WecomInboundEvent,
                WecomInboundEvent.id == WecomOutboundOutbox.inbound_event_id,
            ).filter(
                due,
                no_earlier_unsent_for_user,
                WecomInboundEvent.status == "done",
            )
            if inbound_event_id:
                query = query.filter(
                    WecomOutboundOutbox.inbound_event_id == int(inbound_event_id),
                )
            rows = (
                query.order_by(
                    WecomOutboundOutbox.inbound_event_id,
                    WecomOutboundOutbox.reply_index,
                )
                .with_for_update(skip_locked=True)
                .limit(limit)
                .all()
            )
            claimed: list[dict] = []
            for row in rows:
                row.status = "sending"
                row.locked_at = now
                row.attempt_count = int(row.attempt_count or 0) + 1
                content = row.content
                if row.recommendation_delivery_id:
                    delivery = db.get(RecommendationDelivery, row.recommendation_delivery_id)
                    if not delivery or not delivery.content_ciphertext:
                        raise RuntimeError("recommendation delivery body unavailable")
                    if delivery.status != "pending":
                        raise RuntimeError(
                            f"recommendation delivery not sendable: {delivery.status}"
                        )
                    delivery.status = "sending"
                    delivery.attempt_count = int(delivery.attempt_count or 0) + 1
                    from app.services.recommendation_delivery_service import decrypt_body
                    content = decrypt_body(delivery.content_ciphertext.decode("ascii"))
                claimed.append({
                    "id": int(row.id),
                    "userid": row.userid,
                    "content": content or "",
                    "recommendation_delivery_id": row.recommendation_delivery_id,
                    "attempt_count": int(row.attempt_count),
                })
            db.commit()
            return claimed
        except Exception:
            db.rollback()
            logger.exception("worker: claim outbox failed")
            return []
        finally:
            db.close()

    def _mark_outbox_sent(self, outbox_id: int, provider_msg_id: str | None) -> bool:
        db = SessionLocal()
        try:
            row = db.get(WecomOutboundOutbox, outbox_id)
            delivery_id = row.recommendation_delivery_id if row else None
            updated = db.query(WecomOutboundOutbox).filter(
                WecomOutboundOutbox.id == outbox_id,
                WecomOutboundOutbox.status == "sending",
            ).update({
                "status": "sent",
                "provider_msg_id": (provider_msg_id or "")[:128] or None,
                "sent_at": func.now(6),
                "locked_at": None,
                "next_attempt_at": None,
                "last_error": None,
            })
            if updated == 1 and delivery_id:
                from app.services.recommendation_delivery_service import mark_delivery_sent
                mark_delivery_sent(db, delivery_id, provider_msg_id)
            db.commit()
            if updated == 1 and delivery_id:
                impression_db = SessionLocal()
                try:
                    delivery = impression_db.get(RecommendationDelivery, delivery_id)
                    if delivery:
                        from app.services.recommendation_exposure_service import derive_impressions
                        derive_impressions(impression_db, delivery)
                        impression_db.commit()
                except Exception:
                    impression_db.rollback()
                    logger.exception(
                        "worker: impression derivation deferred delivery_id=%s",
                        delivery_id,
                    )
                finally:
                    impression_db.close()
            return updated == 1
        except Exception:
            db.rollback()
            # Provider may already have accepted the message. Leaving the row in
            # sending makes it recoverable but can duplicate after the stale TTL.
            logger.exception(
                "worker: mark outbox sent failed id=%s; duplicate risk on recovery",
                outbox_id,
            )
            return False
        finally:
            db.close()

    def _mark_outbox_failed(
        self,
        item: dict,
        error: Exception,
        *,
        terminal: bool = False,
    ) -> None:
        db = SessionLocal()
        try:
            attempts = int(item["attempt_count"])
            dead = terminal or attempts >= OUTBOX_MAX_ATTEMPTS
            values: dict = {
                "status": "dead_letter" if dead else "pending",
                "locked_at": None,
                "last_error": f"{type(error).__name__}: {error}"[:1000],
            }
            if dead:
                values["next_attempt_at"] = None
            else:
                backoff = OUTBOX_RETRY_BACKOFFS[
                    min(attempts - 1, len(OUTBOX_RETRY_BACKOFFS) - 1)
                ]
                values["next_attempt_at"] = func.timestampadd(
                    text("SECOND"), backoff, func.now(6),
                )
            db.query(WecomOutboundOutbox).filter(
                WecomOutboundOutbox.id == item["id"],
                WecomOutboundOutbox.status == "sending",
            ).update(values)
            delivery_id = item.get("recommendation_delivery_id")
            if delivery_id:
                db.query(RecommendationDelivery).filter(
                    RecommendationDelivery.delivery_id == delivery_id,
                    RecommendationDelivery.status == "sending",
                ).update({
                    "status": "dead_letter" if dead else "pending",
                    "last_error": values["last_error"],
                }, synchronize_session=False)
            db.commit()
            if dead:
                self._write_send_failed_audit(
                    item["userid"], item["content"], attempts,
                )
        except Exception:
            db.rollback()
            logger.exception(
                "worker: mark outbox failed failed id=%s",
                item.get("id"),
            )
        finally:
            db.close()

    def _deliver_outbox_item(self, item: dict) -> bool:
        try:
            response = self._wecom_client.send_text(
                item["userid"], item["content"],
            )
        except WeComError as exc:
            if getattr(exc, "errcode", 0) == 42001:
                try:
                    self._wecom_client.invalidate_token()
                    response = self._wecom_client.send_text(
                        item["userid"], item["content"],
                    )
                except Exception as retry_exc:
                    self._mark_outbox_failed(item, retry_exc)
                    return False
            elif getattr(exc, "errcode", 0) in (60111, 84061, 40031):
                self._mark_user_inactive(item["userid"])
                self._mark_outbox_failed(item, exc, terminal=True)
                return False
            else:
                self._mark_outbox_failed(item, exc)
                return False
        except Exception as exc:
            self._mark_outbox_failed(item, exc)
            return False

        return self._mark_outbox_sent(
            item["id"],
            response.get("msgid") if isinstance(response, dict) else None,
        )

    def _deliver_outbox_for_event(self, inbound_event_id: Any) -> bool:
        # The per-user NOT EXISTS gate intentionally exposes at most the earliest
        # reply. Loop so a normal multi-part response is still delivered promptly
        # and in order; stop after a failure because that row remains the fence.
        results: list[bool] = []
        while True:
            claimed = self._claim_outbox(inbound_event_id=inbound_event_id)
            if not claimed:
                break
            batch_results = [
                self._deliver_outbox_item(item) for item in claimed
            ]
            results.extend(batch_results)
            if not all(batch_results):
                break
        return all(results)

    def _process_outbox_once(self) -> None:
        for item in self._claim_outbox():
            self._deliver_outbox_item(item)

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
                "worker: recipient unreachable user_hash=%s errcode=%s",
                identifier_hash(reply.userid), errcode,
            )
            self._mark_user_inactive(reply.userid)
            return False

        # API 限流：入 send_retry
        if errcode in (45009, 45018):
            self._enqueue_send_retry(reply, backoff=60)
            return False

        # 其它错误：入 send_retry
        logger.warning(
            "worker: send_text failed, enqueue retry user_hash=%s errcode=%s msg=%s",
            identifier_hash(reply.userid), errcode, exc,
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
            logger.exception(
                "worker: mark_user_inactive failed user_hash=%s",
                identifier_hash(userid),
            )
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
                "worker: rate_limit_notify send failed (drop) user_hash=%s err=%s",
                identifier_hash(userid), exc,
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
            logger.info(
                "worker: send_retry success user_hash=%s retries=%d",
                identifier_hash(userid),
                retry_count,
            )
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
                # 与 created_at 的 MySQL CURRENT_TIMESTAMP 使用同一数据库时钟，
                # 避免容器 UTC 与 DB Asia/Shanghai 混写后 queue latency 变成 -8h。
                "worker_started_at": func.now(6),
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
                "worker_finished_at": func.now(6),
            })
            delivery_ids = db.query(
                WecomOutboundOutbox.recommendation_delivery_id,
            ).filter(
                WecomOutboundOutbox.inbound_event_id == event_id,
                WecomOutboundOutbox.recommendation_delivery_id.isnot(None),
            )
            db.query(RecommendationDelivery).filter(
                RecommendationDelivery.delivery_id.in_(delivery_ids),
                RecommendationDelivery.status == "prepared",
            ).update({"status": "pending"}, synchronize_session=False)
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
                "worker_finished_at": func.now(6),
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
                "status": "processing",
                "error_message": error_msg[:1000],
                "retry_count": retry_count,
                # A Redis requeue failure means no worker still owns this event.
                # Make it eligible at the next periodic scan instead of waiting
                # the full active-processing stale window (180s).
                "worker_started_at": func.timestampadd(
                    text("SECOND"),
                    -(
                        RECOVERY_PROCESSING_STALE_SECONDS
                        - RECOVERY_SCAN_INTERVAL_SECONDS
                    ),
                    func.now(6),
                ),
            })
            db.commit()
            self._last_recovery_scan = time.monotonic()
        except Exception:
            db.rollback()
            logger.exception(
                "worker: update_retry_and_error failed id=%s", event_id,
            )
        finally:
            db.close()

    def _mark_event_recovery_due(
        self,
        event_id: Any,
        *,
        error_message: str | None = None,
    ) -> None:
        """Persist a lost queue handoff for recovery on the next bounded scan."""
        if not event_id:
            return
        db = SessionLocal()
        try:
            values: dict = {
                "status": "processing",
                "worker_started_at": func.timestampadd(
                    text("SECOND"),
                    -(
                        RECOVERY_PROCESSING_STALE_SECONDS
                        - RECOVERY_SCAN_INTERVAL_SECONDS
                    ),
                    func.now(6),
                ),
            }
            if error_message:
                values["error_message"] = error_message[:1000]
            db.query(WecomInboundEvent).filter(
                WecomInboundEvent.id == event_id,
            ).update(values)
            db.commit()
            self._last_recovery_scan = time.monotonic()
        except Exception:
            db.rollback()
            logger.exception(
                "worker: failed to make event recovery-due id=%s",
                event_id,
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
                        content=(
                            "[recommendation_delivery]"
                            if reply.recommendation_context
                            else reply.content
                        ),
                        wecom_msg_id=None,
                        intent=reply.intent,
                        criteria_snapshot=reply.criteria_snapshot,
                        recommendation_delivery_id=reply.delivery_id,
                        redaction_state=(
                            "encrypted_delivery"
                            if reply.recommendation_context
                            else None
                        ),
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
        "_enqueued_at": time.time(),
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
    configure_loguru(os.getenv("APP_ENV", "development"))
    Worker().start()


if __name__ == "__main__":
    main()
