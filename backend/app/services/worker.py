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

import copy
import json
import logging
import os
import signal
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import and_, exists, func, null, or_, text
from sqlalchemy.orm import Session, aliased

from app.config import settings
from app.core.redis_client import (
    QUEUE_DEAD_LETTER,
    QUEUE_INCOMING,
    QUEUE_RATE_LIMIT_NOTIFY,
    QUEUE_SEND_RETRY,
    SESSION_TTL,
    SessionCommitDeadlineExceeded,
    UserLockUnavailable,
    enqueue_message,
    get_redis,
    user_lock,
    validate_redis_durability_policy,
)
from app.core.logging_setup import configure_loguru, identifier_hash
from app.db import SessionLocal
from app.models import (
    AuditLog,
    ConversationLog,
    Job,
    Resume,
    WecomInboundEvent,
    WecomOutboundOutbox,
    RecommendationDelivery,
    ContactDelivery,
    User,
)
from app.schemas.conversation import ReplyMessage
from app.services import (
    action_gateway,
    action_execution_service,
    action_parse_artifact_service,
    conversation_service,
    message_router,
    recommendation_shadow_service,
    search_service,
    user_service,
    upload_service,
)
from app.tasks.common import log_event
from app.wecom.callback import WeComMessage
from app.wecom.client import (
    WeComClient,
    WeComError,
    parse_invalid_recipients,
    recipient_rejected,
    whitelist_send_response,
)

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
AUX_QUEUE_BATCH_SIZE = 100
RECOVERY_SCAN_INTERVAL_SECONDS = 30
RECOVERY_RECEIVED_STALE_SECONDS = 10
RECOVERY_PROCESSING_STALE_SECONDS = 180
DISPATCHER_LEASE_SECONDS = 180
OUTBOX_SENDING_STALE_SECONDS = 180
OUTBOX_CLAIM_BATCH_SIZE = 20
OUTBOX_MAX_ATTEMPTS = 5
OUTBOX_RETRY_BACKOFFS = [30, 60, 120, 300, 600]
SESSION_COMMIT_STALE_SECONDS = 180
SESSION_COMMIT_RETRY_BACKOFFS = [5, 15, 30, 60, 300]

# §9.6 行 1921 的 recommendation_delivery.status 合法枚举。这里是唯一的写入口径，
# 历史实现里的 dead_letter/expired/redacted 一律不再写入。
DELIVERY_PREPARED = "prepared"
DELIVERY_PENDING = "pending"
DELIVERY_SENDING = "sending"
DELIVERY_RETRY_WAIT = "retry_wait"
DELIVERY_SENT = "sent"
DELIVERY_PERMANENT_FAILED = "permanent_failed"
DELIVERY_UNKNOWN = "unknown"
# §10.4.1：顺序门禁上的“未完成”集合；sent/permanent_failed/unknown 是 terminal。
DELIVERY_ACTIVE_STATUSES = (
    DELIVERY_PREPARED, DELIVERY_PENDING, DELIVERY_SENDING, DELIVERY_RETRY_WAIT,
)
# §10.4 的 claim 条件：只有 pending/retry_wait 允许被 dispatcher 领走发送。
DELIVERY_SENDABLE_STATUSES = (DELIVERY_PENDING, DELIVERY_RETRY_WAIT)


def _recommendation_target_references(
    raw_context: Any,
) -> tuple[list[tuple[str, int]] | None, str | None, str | None]:
    """Strictly decode the target set that guards a recommendation claim.

    The persisted JSON is part of the send authorization boundary.  Returning
    an empty/partial set would let the dispatcher send without locking every
    target represented in the recommendation body, so every malformed shape
    is an explicit permanent failure.
    """
    context = raw_context
    if isinstance(context, str):
        try:
            context = json.loads(context)
        except (TypeError, ValueError):
            return None, "context_parse_failed", "recommendation context is not valid JSON"

    if not isinstance(context, dict):
        return None, "context_not_object", "recommendation context must be a JSON object"
    if "items" not in context:
        return None, "context_items_missing", "recommendation context items are missing"

    items = context["items"]
    if not isinstance(items, list):
        return None, "context_items_invalid", "recommendation context items must be a JSON array"
    if not items:
        return None, "context_targets_missing", "recommendation context contains no targets"

    references: list[tuple[str, int]] = []
    for item in items:
        if not isinstance(item, dict):
            return None, "context_item_invalid", "recommendation context contains a non-object item"
        target_type = item.get("target_type")
        target_id = item.get("target_id")
        if (
            target_type not in ("job", "resume")
            or isinstance(target_id, bool)
            or not isinstance(target_id, int)
            or target_id <= 0
        ):
            return None, "context_item_invalid", "recommendation context contains an invalid target"
        references.append((target_type, target_id))

    return sorted(set(references)), None, None


def _build_outbox_claim_query(
    db: Session,
    due,
    *,
    inbound_event_id: Any = None,
    outbox_id: int | None = None,
    limit: int = OUTBOX_CLAIM_BATCH_SIZE,
    lock_rows: bool = True,
    check_inbound_done: bool = True,
):
    earlier = aliased(WecomOutboundOutbox)
    no_earlier_unsent_for_user = ~exists().where(and_(
        earlier.userid == WecomOutboundOutbox.userid,
        earlier.id < WecomOutboundOutbox.id,
        earlier.status.in_(("pending", "sending")),
    ))
    current_delivery = aliased(RecommendationDelivery)
    earlier_delivery = aliased(RecommendationDelivery)
    no_earlier_active_delivery = ~exists().where(and_(
        current_delivery.delivery_id
        == WecomOutboundOutbox.recommendation_delivery_id,
        earlier_delivery.userid == current_delivery.userid,
        earlier_delivery.delivery_order < current_delivery.delivery_order,
        earlier_delivery.status.in_(DELIVERY_ACTIVE_STATUSES),
    ))
    predicates = [
        due,
        no_earlier_unsent_for_user,
        no_earlier_active_delivery,
    ]
    if check_inbound_done:
        # Keep this predicate in discovery reads.  Locking reads disable it
        # for the claim transaction because MySQL may lock/skip the matching
        # inbound row under FOR UPDATE SKIP LOCKED; the claim path performs a
        # separate consistent read after locking only the outbox row.
        predicates.append(exists().where(and_(
            WecomInboundEvent.id == WecomOutboundOutbox.inbound_event_id,
            WecomInboundEvent.status == "done",
        )))
    query = db.query(WecomOutboundOutbox).filter(*predicates)
    if inbound_event_id:
        query = query.filter(
            WecomOutboundOutbox.inbound_event_id == int(inbound_event_id),
        )
    if outbox_id is not None:
        query = query.filter(WecomOutboundOutbox.id == int(outbox_id))
    query = query.order_by(WecomOutboundOutbox.id)
    if lock_rows:
        query = query.with_for_update(skip_locked=True)
    return query.limit(limit)

# §10.4.1：dispatcher / prepared session reconciler 各自独立 250ms 扫描，
# 每批最多 100 条；§10.5 的 impression deriver 同样 250ms。
AUX_LOOP_INTERVAL_SECONDS = 0.25
AUX_LOOP_BATCH_SIZE = 100
# §10.5：sent 提交后立即投递到有界 executor，用户锁释放前最多等待 200ms。
IMPRESSION_IMMEDIATE_WAIT_SECONDS = 0.2
IMPRESSION_IMMEDIATE_BATCH_SIZE = 20
IMPRESSION_EXECUTOR_WORKERS = 2
# §10.3：拿到成功响应后必须优先、带有限重试地提交 sent；数据库暂时不可用时
# 只在进程内重试并告警，绝不重新调用企微。
SEND_COMMIT_MAX_ATTEMPTS = 5
SEND_COMMIT_BACKOFFS = [0.2, 0.5, 1.0, 2.0]

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
        # 每个进程一个稳定且全局唯一的 lease owner。所有 delivery 状态转移都带
        # `lease_owner=?` 条件，租约过期后被别人接管的旧 Worker 无法再覆盖新状态
        # （§10.4「状态转移使用条件 UPDATE，防止旧 Worker 覆盖新状态」）。
        self._lease_owner = f"worker-{self._pid}-{uuid.uuid4().hex[:8]}"[:64]
        self._aux_threads: list[threading.Thread] = []
        self._impression_executor: ThreadPoolExecutor | None = None

    # -----------------------------------------------------------------------
    # 启停
    # -----------------------------------------------------------------------

    def start(self) -> None:
        validate_redis_durability_policy(self._redis)
        logger.info("worker: starting pid=%d", self._pid)
        from app.services.recommendation_shadow_service import start_shadow_runner
        from app.services.recommendation_strategy_service import (
            start_runtime_control_watcher,
            stop_runtime_control_watcher,
        )

        start_shadow_runner()
        start_runtime_control_watcher()
        self._setup_signal_handlers()
        self._start_heartbeat()
        self._startup_recovery()
        self._start_aux_loops()
        try:
            self._main_loop()
        finally:
            self._stop_aux_loops()
            from app.services.recommendation_shadow_service import shutdown_shadow_runner

            stop_runtime_control_watcher()
            shutdown_shadow_runner()
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
        from app.services.phase11_build_info import build_probe_payload

        heartbeat_payload = json.dumps(
            build_probe_payload(), separators=(",", ":"), sort_keys=True,
        )

        def _hb() -> None:
            while self._running:
                try:
                    self._redis.set(
                        f"worker:heartbeat:{self._pid}", heartbeat_payload,
                        ex=HEARTBEAT_TTL,
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
    # 独立恢复线程（§10.4.1 / §10.5）
    # -----------------------------------------------------------------------

    def _start_interval_thread(
        self, name: str, fn: Callable[[], Any], interval: float,
    ) -> threading.Thread:
        """固定间隔循环线程；单次迭代异常不会杀死线程。"""
        def _loop() -> None:
            while self._running:
                started = time.monotonic()
                try:
                    fn()
                except Exception:
                    logger.exception("worker: %s iteration failed", name)
                time.sleep(max(0.0, interval - (time.monotonic() - started)))

        thread = threading.Thread(target=_loop, daemon=True, name=name)
        thread.start()
        return thread

    def _start_aux_loops(self) -> None:
        """启动 dispatcher / session reconciler / impression deriver 三条独立线程。

        §10.4.1 明确要求它们不读取 incoming 队列：恢复扫描一旦挂在主循环上，
        一条慢 LLM 消息就能让整个 delivery 恢复停摆。有界 impression executor
        用于 §10.5 的 sent 后即时派生，同样只走 claim 流程。
        """
        from app.tasks import (
            recommendation_delivery_dispatcher,
            recommendation_impression_deriver,
            recommendation_session_reconciler,
        )

        self._impression_executor = ThreadPoolExecutor(
            max_workers=IMPRESSION_EXECUTOR_WORKERS,
            thread_name_prefix="impression-executor",
        )
        self._aux_threads = [
            self._start_interval_thread(
                "inbound-event-dispatcher",
                self._dispatch_received_events_once,
                RECOVERY_SCAN_INTERVAL_SECONDS,
            ),
            self._start_interval_thread(
                "delivery-dispatcher",
                lambda: recommendation_delivery_dispatcher.run_once(self),
                recommendation_delivery_dispatcher.SCAN_INTERVAL_SECONDS,
            ),
            self._start_interval_thread(
                "session-reconciler",
                lambda: recommendation_session_reconciler.run_once(self),
                recommendation_session_reconciler.SCAN_INTERVAL_SECONDS,
            ),
            self._start_interval_thread(
                "impression-deriver",
                lambda: recommendation_impression_deriver.run_once(self),
                recommendation_impression_deriver.SCAN_INTERVAL_SECONDS,
            ),
        ]

    def _stop_aux_loops(self) -> None:
        executor, self._impression_executor = self._impression_executor, None
        if executor is not None:
            executor.shutdown(wait=False)
        for thread in self._aux_threads:
            thread.join(timeout=AUX_LOOP_INTERVAL_SECONDS * 4)
        self._aux_threads = []

    def _dispatch_received_events_once(self) -> int:
        """Reconcile accepted durable inbox rows whose Redis enqueue was lost.

        The inbox row remains ``received`` until the normal Worker claims it;
        therefore a Redis outage never turns into an acknowledged, invisible
        message. Duplicate queue payloads are harmless because processing has
        a terminal durable-event gate.
        """
        db = SessionLocal()
        try:
            lease_due = or_(
                WecomInboundEvent.dispatcher_lease_owner.is_(None),
                WecomInboundEvent.dispatcher_lease_expires_at <= func.now(6),
            )
            rows = (
                db.query(WecomInboundEvent)
                .filter(
                    WecomInboundEvent.status == "received",
                    WecomInboundEvent.rate_limit_decision == "accepted",
                    lease_due,
                )
                .order_by(WecomInboundEvent.id)
                .with_for_update(skip_locked=True)
                .limit(AUX_QUEUE_BATCH_SIZE)
                .all()
            )
            for row in rows:
                row.dispatcher_lease_owner = self._lease_owner
                row.dispatcher_lease_expires_at = func.timestampadd(
                    text("SECOND"), DISPATCHER_LEASE_SECONDS, func.now(6),
                )
            # The lease must be durable before Redis I/O. A second dispatcher
            # therefore skips these rows even while the first one is enqueueing.
            db.commit()
            dispatched = 0
            for row in rows:
                try:
                    enqueue_message(
                        json.dumps(_inbound_event_to_queue_msg(row), ensure_ascii=False),
                        QUEUE_INCOMING,
                    )
                    dispatched += 1
                except Exception:
                    logger.exception(
                        "worker: inbound dispatcher enqueue failed event_id=%s",
                        row.id,
                    )
                    self._release_dispatcher_lease(row.id)
            return dispatched
        except Exception:
            db.rollback()
            logger.exception("worker: inbound dispatcher scan failed")
            return 0
        finally:
            db.close()

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
                # Keep the legacy recovery query shape/status contract. Rows
                # explicitly marked rate_limited are terminal and must never
                # enter the business queue; pre-migration rows have no
                # decision value and remain recoverable as accepted.
                if getattr(row, "rate_limit_decision", None) == "rate_limited":
                    continue
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
        # delivery dispatcher / session reconciler / impression deriver 已经是独立
        # 线程（§10.4.1），主循环只保留纯 Redis 队列的即发即弃任务，否则慢消息期间
        # 恢复扫描仍然会被 incoming 队列拖住。
        self._process_rate_limit_notify_once()
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
        submitted_shadow_request_ids: set[str] = set()
        committed_shadow_request_ids: set[str] = set()
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

            # Workstream A pre-routing: parse once before entering the Router.
            # The default remains off; legacy turns never create Action rows.
            action_context = None
            action_claim = None
            preloaded_user_context = None
            mode = getattr(settings, "action_execution_mode", "off")
            if msg.msg_type == "text" and mode in {"on", "shadow"}:
                gateway = action_gateway.ActionGateway()
                # The Router normally auto-registers users, but ActionGateway
                # runs before Router.  Resolve the same registration boundary
                # here so a first-touch worker can claim a search Action.
                gateway_user = user_service.identify_or_register(userid, db)
                preloaded_user_context = gateway_user
                role = gateway_user.role
                session_hint = conversation_service.load_session(userid)
                envelope = gateway.classify(
                    msg, session=session_hint,
                    actor=type("GatewayActor", (), {"role": role or ""})(),
                )
                percentage = int(getattr(settings, "action_execution_rollout_percentage", 0) or 0)
                in_bucket = percentage >= 100 or (
                    percentage > 0 and int(identifier_hash(userid)[:8], 16) % 100 < percentage
                )
                if mode == "on" and in_bucket and envelope.is_supported:
                    if envelope.parse_ref and envelope.parse_payload and envelope.parse_digest:
                        try:
                            action_parse_artifact_service.persist_parse_artifact(
                                db, parse_ref=envelope.parse_ref, turn_id=envelope.turn_id,
                                actor_userid=userid, payload=envelope.parse_payload,
                                parse_digest_value=envelope.parse_digest,
                                classifier_version=envelope.classifier_version,
                                session_version=envelope.session_version,
                                schema_version=envelope.parse_schema_version or action_parse_artifact_service.PARSE_SCHEMA_VERSION,
                                expires_at=envelope.parse_expires_at,
                                retention_seconds=int(getattr(settings, "action_parse_artifact_retention_seconds", 86400)),
                            )
                            db.commit()
                        except Exception:
                            db.rollback()
                            return "action_parse_artifact_missing"
                    action_claim = action_execution_service.claim_action_execution(
                        db, envelope.turn_id, envelope.action_name, self._lease_owner,
                        request_digest=envelope.request_digest,
                        actor_userid=userid,
                        lease_seconds=int(getattr(settings, "action_execution_lease_seconds", 180)),
                        parse_ref=envelope.parse_ref, parse_digest=envelope.parse_digest,
                        parse_version=envelope.parse_schema_version,
                        parse_expires_at=envelope.parse_expires_at,
                    )
                    db.commit()
                    if action_claim.replay:
                        try:
                            action_execution_service.load_replay_reference(
                                db, envelope.turn_id, envelope.action_name,
                                actor_userid=userid,
                            )
                            replay_row = action_execution_service.read_action_execution(
                                db, envelope.turn_id, envelope.action_name,
                            )
                            if replay_row is not None and replay_row.parse_ref:
                                artifact = action_parse_artifact_service.read_parse_artifact(
                                    db, replay_row.parse_ref, turn_id=envelope.turn_id,
                                    actor_userid=userid, parse_digest_value=replay_row.parse_digest,
                                    schema_version=replay_row.parse_version or action_parse_artifact_service.PARSE_SCHEMA_VERSION,
                                )
                                if artifact is None:
                                    return "action_replay_terminal"
                        except Exception:
                            return "action_replay_terminal"
                        if inbound_event_id:
                            self._apply_session_commit_for_event(inbound_event_id, userid)
                            self._deliver_outbox_for_event(inbound_event_id)
                        return "action_replayed"
                    if action_claim.busy:
                        return "action_in_progress"
                    if action_claim.state == "failed_terminal":
                        return "action_terminal"
                    action_context = envelope
                elif mode in {"on", "shadow"}:
                    # Keep the same parse for compatible legacy routing; no claim.
                    if envelope.parse_ref and envelope.parse_payload and envelope.parse_digest:
                        try:
                            action_parse_artifact_service.persist_parse_artifact(
                                db, parse_ref=envelope.parse_ref, turn_id=envelope.turn_id,
                                actor_userid=userid, payload=envelope.parse_payload,
                                parse_digest_value=envelope.parse_digest,
                                classifier_version=envelope.classifier_version,
                                session_version=envelope.session_version,
                                schema_version=envelope.parse_schema_version or action_parse_artifact_service.PARSE_SCHEMA_VERSION,
                                expires_at=envelope.parse_expires_at,
                                retention_seconds=int(getattr(settings, "action_parse_artifact_retention_seconds", 86400)),
                            )
                            db.commit()
                        except Exception:
                            db.rollback()
                            if mode == "on":
                                return "action_parse_artifact_missing"
                    action_context = envelope

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
            shadow_tracking_token = (
                recommendation_shadow_service.begin_turn_tracking()
            )
            staged_session = None
            try:
                replies = message_router.process(
                    msg,
                    db,
                    action_context=action_context,
                    user_context=preloaded_user_context,
                    inbound_event_id=inbound_event_id,
                )
            finally:
                submitted_shadow_request_ids.update(
                    recommendation_shadow_service.end_turn_tracking(
                        shadow_tracking_token,
                    ),
                )
                staged_session = conversation_service.end_session_staging(
                    stage_token,
                )
                search_service.reset_queue_backlog_hint(backlog_token)
            committed_shadow_request_ids = {
                reply.recommendation_request.request_id
                for reply in replies
                if (
                    reply.recommendation_request is not None
                    and reply.recommendation_request.execution_mode == "shadow"
                    and reply.recommendation_request.request_id
                )
            }

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
            if action_claim is not None and action_claim.acquired:
                request_ids = [
                    reply.recommendation_request.request_id
                    for reply in replies
                    if reply.recommendation_request is not None
                    and reply.recommendation_request.request_id
                ]
                snapshot_ids = [
                    reply.recommendation_request.snapshot_id
                    for reply in replies
                    if reply.recommendation_request is not None
                    and reply.recommendation_request.snapshot_id
                ]
                delivery_ids = [reply.delivery_id for reply in replies if reply.delivery_id]
                reference = action_execution_service.build_result_reference(
                    turn_id=action_claim.row.turn_id,
                    action_name=action_claim.row.action_name,
                    request_id=request_ids[-1] if request_ids else None,
                    snapshot_id=snapshot_ids[-1] if snapshot_ids else None,
                    delivery_ids=delivery_ids,
                    result_ref_type="recommendation" if request_ids or delivery_ids else "terminal",
                )
                if not action_execution_service.finalize_action_execution(
                    db, action_claim.row.turn_id, action_claim.row.action_name,
                    self._lease_owner, action_claim.fencing_token,
                    result_reference=reference,
                    result_digest=action_context.request_digest if action_context else None,
                ):
                    raise RuntimeError("action_fence_lost")
            db.commit()
            business_committed = True
            # A shadow callback may persist only after the served request/outbox
            # transaction exists.  Direct/no-event calls do not stage those
            # facts, so their detached observations are discarded.
            for shadow_request_id in (
                submitted_shadow_request_ids | committed_shadow_request_ids
            ):
                if (
                    inbound_event_id
                    and shadow_request_id in committed_shadow_request_ids
                ):
                    recommendation_shadow_service.activate_persistence(
                        shadow_request_id,
                    )
                else:
                    recommendation_shadow_service.discard(shadow_request_id)

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
            if not business_committed:
                for shadow_request_id in (
                    submitted_shadow_request_ids
                    | committed_shadow_request_ids
                ):
                    recommendation_shadow_service.discard(shadow_request_id)
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

    @staticmethod
    def _media_operation_id(msg: WeComMessage) -> str | None:
        session = conversation_service.load_session(msg.from_user)
        if session is None:
            return msg.msg_id
        if (
            session.pending_upload_intent
            and upload_service.is_pending_upload_expired(session)
        ):
            msg.expired_upload_draft = True
            return None
        if session.pending_upload_mode != "replace":
            return msg.msg_id
        valid_target = (
            session.pending_upload_intent == "upload_job"
            and type(session.pending_target_id) is int
            and session.pending_target_id > 0
            and type(session.pending_target_version) is int
            and session.pending_target_version > 0
        )
        if not valid_target or upload_service.is_pending_upload_expired(session):
            raise RuntimeError("replacement_media_context_invalid")
        operation_id = session.pending_operation_id
        if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 36:
            raise RuntimeError("replacement_media_operation_id_invalid")
        return operation_id

    def _download_and_attach_image(self, msg: WeComMessage) -> None:
        media_id = None
        try:
            from app.storage import get_storage
            from app.services.job_media_service import record_pending_media

            operation_id = self._media_operation_id(msg)
            if operation_id is None:
                return
            blob = self._wecom_client.download_media(msg.media_id)
            storage = get_storage()
            key = f"images/{msg.from_user}/{msg.msg_id}.jpg"
            with SessionLocal() as media_db:
                media = record_pending_media(
                    media_db,
                    key,
                    owner_userid=msg.from_user,
                    operation_id=operation_id,
                )
                media_db.commit()
                media_id = media.id
            storage.save(key, blob, content_type="image/jpeg")
            msg.image_url = key
            msg.media_lifecycle_id = media_id
        except Exception:
            if media_id is not None:
                from app.services.job_media_service import mark_delete_pending
                with SessionLocal() as media_db:
                    mark_delete_pending(media_db, [media_id])
                    media_db.commit()
            logger.exception(
                "worker: image download/save failed media_id=%s msg_id=%s",
                msg.media_id, msg.msg_id,
            )
            msg.image_url = ""
            msg.media_lifecycle_id = None

    # -----------------------------------------------------------------------
    # 回复发送（失败补偿）
    # -----------------------------------------------------------------------

    def _stage_session_commit(
        self,
        db: Session,
        inbound_event_id: Any,
        commit: conversation_service.StagedSessionCommit,
    ) -> None:
        # P1-14/§10.1.1：staged payload 含搜索条件、候选快照和 history，绝不能以明文
        # JSON 落 wecom_inbound_event。本轮存在推荐 delivery 时改写加密的
        # session_patch_ciphertext，inbound 事件只保留 operation/expected_version。
        patched = self._stage_session_patch(db, inbound_event_id, commit)
        updated = db.query(WecomInboundEvent).filter(
            WecomInboundEvent.id == inbound_event_id,
        ).update({
            "status": "session_pending",
            "session_operation": commit.operation,
            "session_expected_version": commit.expected_version,
            "session_payload": null() if patched else commit.payload,
            "session_apply_attempts": 0,
            "session_apply_locked_at": None,
            "session_apply_lease_owner": None,
            "session_next_attempt_at": None,
            "session_commit_deadline_epoch": (
                func.unix_timestamp(func.now(6)) + SESSION_TTL
            ),
            "session_applied_at": None,
            "worker_finished_at": None,
        })
        if updated != 1:
            raise RuntimeError(
                f"unable to stage session commit for event {inbound_event_id}",
            )

    def _stage_session_patch(
        self,
        db: Session,
        inbound_event_id: Any,
        commit: conversation_service.StagedSessionCommit,
    ) -> bool:
        """把 session patch 加密写入本轮 delivery（§9.6 / §10.1.1）。

        返回是否已经落到密文列。非推荐回合没有 delivery 行可承载 patch，保持既有
        非推荐合同（§10.1 行 2210）不变。
        """
        if commit.payload is None:
            return False
        deliveries = self._deliveries_for_event(db, inbound_event_id)
        if not deliveries:
            return False
        from app.services.recommendation_delivery_service import store_session_patch

        payload = json.dumps(commit.payload, ensure_ascii=False)
        for delivery in deliveries:
            store_session_patch(delivery, payload)
            delivery.session_expected_version = int(commit.expected_version or 0)
            delivery.session_commit_state = "not_applied"
        return True

    @staticmethod
    def _deliveries_for_event(
        db: Session, inbound_event_id: Any,
    ) -> list[RecommendationDelivery]:
        """本轮 inbound 事件关联的推荐 delivery，按回复顺序返回。"""
        if not inbound_event_id:
            return []
        return (
            db.query(RecommendationDelivery)
            .join(
                WecomOutboundOutbox,
                WecomOutboundOutbox.recommendation_delivery_id
                == RecommendationDelivery.delivery_id,
            )
            .filter(WecomOutboundOutbox.inbound_event_id == int(inbound_event_id))
            .order_by(WecomOutboundOutbox.reply_index)
            .all()
        )

    @staticmethod
    def _lock_outboxes_for_event(
        db: Session, inbound_event_id: Any,
    ) -> list[WecomOutboundOutbox]:
        if not inbound_event_id:
            return []
        return (
            db.query(WecomOutboundOutbox)
            .filter(WecomOutboundOutbox.inbound_event_id == int(inbound_event_id))
            .order_by(WecomOutboundOutbox.id)
            .with_for_update()
            .all()
        )

    @staticmethod
    def _lock_deliveries_by_id(
        db: Session, delivery_ids: list[str],
    ) -> list[RecommendationDelivery]:
        if not delivery_ids:
            return []
        return (
            db.query(RecommendationDelivery)
            .populate_existing()
            .filter(RecommendationDelivery.delivery_id.in_(delivery_ids))
            .order_by(RecommendationDelivery.delivery_id)
            .with_for_update()
            .all()
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
            deadline_epoch_value = func.coalesce(
                WecomInboundEvent.session_commit_deadline_epoch,
                func.unix_timestamp(func.coalesce(
                    WecomInboundEvent.worker_started_at,
                    WecomInboundEvent.created_at,
                )) + SESSION_TTL,
            )
            deadline_epoch = deadline_epoch_value.label(
                "session_commit_deadline_epoch",
            )
            deadline_reached_value = (
                deadline_epoch_value <= func.unix_timestamp(now)
            )
            deadline_reached = deadline_reached_value.label(
                "session_commit_deadline_reached",
            )
            query = db.query(
                WecomInboundEvent,
                deadline_epoch,
                deadline_reached,
            ).filter(
                WecomInboundEvent.status == "session_pending",
                or_(
                    or_(
                        WecomInboundEvent.session_next_attempt_at.is_(None),
                        WecomInboundEvent.session_next_attempt_at <= now,
                    ),
                    deadline_reached_value,
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
            for row, raw_deadline_epoch, reached in rows:
                operation = str(row.session_operation or "")
                payload_available = True
                try:
                    payload = self._load_session_patch(db, row)
                except Exception as exc:
                    # §10.6：解密失败 fail-closed，保持 prepared 并告警，绝不走明文旁路。
                    if not reached:
                        self._backoff_session_commit(row, exc)
                        continue
                    payload = None
                    payload_available = operation == "delete"
                if operation == "save" and payload is None:
                    payload_available = False
                    if not reached:
                        self._backoff_session_commit(
                            row,
                            RuntimeError("durable session save payload is missing"),
                        )
                        continue
                claim_owner = uuid.uuid4().hex
                row.session_apply_locked_at = now
                row.session_apply_lease_owner = claim_owner
                row.session_apply_attempts = int(
                    row.session_apply_attempts or 0,
                ) + 1
                claimed.append({
                    "event_id": int(row.id),
                    "userid": row.from_userid,
                    "attempts": int(row.session_apply_attempts),
                    "lease_owner": claim_owner,
                    "deadline_epoch": raw_deadline_epoch,
                    "deadline_reached": bool(reached),
                    "payload_available": payload_available,
                    "commit": conversation_service.StagedSessionCommit(
                        userid=row.from_userid,
                        operation=operation,
                        expected_version=int(
                            row.session_expected_version or 0,
                        ),
                        payload=payload,
                        deadline_epoch=raw_deadline_epoch,
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

    def _load_session_patch(
        self, db: Session, row: WecomInboundEvent,
    ) -> dict | None:
        """还原 staged session payload。

        推荐回合的 patch 只存在于 delivery 的 `session_patch_ciphertext`（P1-14），
        非推荐回合继续读 inbound 事件上的 payload。
        """
        if row.session_payload is not None:
            return dict(row.session_payload)
        for delivery in self._deliveries_for_event(db, row.id):
            if not delivery.session_patch_ciphertext:
                continue
            return _decrypt_session_patch(delivery)
        return None

    def _backoff_session_commit(
        self, row: WecomInboundEvent, error: Exception,
    ) -> None:
        """在 claim 事务内推迟一条 session_pending 事件，不改变其 prepared 语义。"""
        attempts = int(row.session_apply_attempts or 0) + 1
        backoff = SESSION_COMMIT_RETRY_BACKOFFS[
            min(attempts - 1, len(SESSION_COMMIT_RETRY_BACKOFFS) - 1)
        ]
        row.session_apply_attempts = attempts
        row.session_apply_locked_at = None
        row.session_apply_lease_owner = None
        row.session_next_attempt_at = func.timestampadd(
            text("SECOND"), backoff, func.now(6),
        )
        row.error_message = f"{type(error).__name__}: {error}"[:1000]
        log_event(
            "recommendation_session_patch_unreadable",
            inbound_event_id=int(row.id),
            attempts=attempts,
            error_type=type(error).__name__,
            severity="alert",
        )

    @staticmethod
    def _current_session_claim_filters(event_id: int, lease_owner: str) -> tuple:
        stale_before = func.timestampadd(
            text("SECOND"), -SESSION_COMMIT_STALE_SECONDS, func.now(6),
        )
        return (
            WecomInboundEvent.id == event_id,
            WecomInboundEvent.status == "session_pending",
            WecomInboundEvent.session_apply_lease_owner == lease_owner,
            WecomInboundEvent.session_apply_locked_at.is_not(None),
            WecomInboundEvent.session_apply_locked_at > stale_before,
        )

    def _mark_session_commit_applied(
        self, event_id: int, lease_owner: str,
    ) -> bool:
        db = SessionLocal()
        try:
            updated = db.query(WecomInboundEvent).filter(
                *self._current_session_claim_filters(event_id, lease_owner),
            ).update({
                "status": "done",
                # The payload can contain transient search/draft PII. It is only
                # needed until Redis confirms the transition, so do not retain it
                # for the normal inbound-event TTL.
                "session_operation": None,
                "session_expected_version": None,
                "session_payload": null(),
                "session_apply_locked_at": None,
                "session_apply_lease_owner": None,
                "session_next_attempt_at": None,
                "session_applied_at": func.now(6),
                "worker_finished_at": func.now(6),
                "error_message": None,
            })
            if updated == 1:
                _promote_prepared_deliveries(db, event_id)
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

    def _mark_session_commit_retry(self, item: dict, error: Exception) -> bool:
        db = SessionLocal()
        try:
            attempts = int(item["attempts"])
            backoff = SESSION_COMMIT_RETRY_BACKOFFS[
                min(attempts - 1, len(SESSION_COMMIT_RETRY_BACKOFFS) - 1)
            ]
            updated = db.query(WecomInboundEvent).filter(
                *self._current_session_claim_filters(
                    item["event_id"], item["lease_owner"],
                ),
            ).update({
                "session_apply_locked_at": None,
                "session_apply_lease_owner": None,
                "session_next_attempt_at": func.timestampadd(
                    text("SECOND"), backoff, func.now(6),
                ),
                "error_message": f"{type(error).__name__}: {error}"[:1000],
            })
            db.commit()
            return updated == 1
        except Exception:
            db.rollback()
            logger.exception(
                "worker: persist session commit retry failed event_id=%s",
                item.get("event_id"),
            )
            return False
        finally:
            db.close()

    def _terminalize_session_commit_locked(
        self,
        db: Session,
        row: WecomInboundEvent,
        *,
        error_code: str,
        error: Exception,
    ) -> None:
        from app.services.recommendation_delivery_service import (
            purge_delivery_content,
        )

        outboxes = self._lock_outboxes_for_event(db, row.id)
        delivery_ids = sorted({
            str(outbox.recommendation_delivery_id)
            for outbox in outboxes
            if outbox.recommendation_delivery_id is not None
        })
        for delivery in self._lock_deliveries_by_id(db, delivery_ids):
            if delivery.status in (
                DELIVERY_PREPARED,
                DELIVERY_PENDING,
                DELIVERY_RETRY_WAIT,
            ):
                delivery.status = DELIVERY_PERMANENT_FAILED
                delivery.last_error_code = error_code[:32]
                delivery.last_error = str(error)[:500]
                delivery.lease_owner = None
                delivery.lease_expires_at = None
            elif delivery.status == DELIVERY_SENDING:
                delivery.status = DELIVERY_UNKNOWN
                delivery.last_error_code = error_code[:32]
                delivery.last_error = str(error)[:500]
                delivery.lease_owner = None
                delivery.lease_expires_at = None
            purge_delivery_content(delivery)

        db.flush()
        for outbox in outboxes:
            if outbox.status not in ("pending", "sending"):
                continue
            was_sending = outbox.status == "sending"
            outbox.status = "dead_letter"
            outbox.locked_at = None
            outbox.next_attempt_at = None
            outbox.last_error = (
                "ambiguous provider outcome; session commit terminalized"
                if was_sending
                else f"session commit terminalized: {error_code}"
            )[:1000]

        row.status = "dead_letter"
        row.session_operation = None
        row.session_expected_version = None
        row.session_payload = None
        row.session_apply_locked_at = None
        row.session_apply_lease_owner = None
        row.session_next_attempt_at = None
        row.session_commit_deadline_epoch = None
        row.worker_finished_at = func.now(6)
        row.error_message = f"{type(error).__name__}: {error}"[:1000]

    def _mark_session_commit_terminal(
        self,
        item: dict,
        *,
        error_code: str,
        error: Exception,
    ) -> bool:
        db = SessionLocal()
        try:
            row = db.query(WecomInboundEvent).filter(
                *self._current_session_claim_filters(
                    item["event_id"], item["lease_owner"],
                ),
            ).with_for_update().first()
            if row is None:
                db.rollback()
                return False
            self._terminalize_session_commit_locked(
                db,
                row,
                error_code=error_code,
                error=error,
            )
            db.commit()
            log_event(
                "session_commit_terminal",
                inbound_event_id=int(row.id),
                error_code=error_code,
                severity="alert",
            )
            return True
        except Exception:
            db.rollback()
            logger.exception(
                "worker: terminalize durable session commit failed event_id=%s",
                item.get("event_id"),
            )
            return False
        finally:
            db.close()

    def _finish_expired_session_commit(
        self,
        item: dict,
        error: Exception,
    ) -> bool:
        if item.get("payload_available", True):
            try:
                if conversation_service.is_staged_session_applied(item["commit"]):
                    return self._mark_session_commit_applied(
                        item["event_id"], item["lease_owner"],
                    )
            except Exception as check_error:
                error = RuntimeError(
                    "durable session commit deadline reached and Redis state "
                    f"could not be verified: {check_error}"
                )
        self._mark_session_commit_terminal(
            item,
            error_code="session_commit_deadline",
            error=error,
        )
        return False

    def _apply_session_commit_item(self, item: dict) -> bool:
        commit = item["commit"]
        if item.get("deadline_reached", False):
            return self._finish_expired_session_commit(
                item,
                RuntimeError("durable session commit deadline exceeded"),
            )
        try:
            applied = conversation_service.apply_staged_session(commit)
            if not applied:
                applied = conversation_service.is_staged_session_applied(commit)
            if not applied:
                raise conversation_service.SessionVersionConflict(
                    "durable session CAS rejected",
                )
        except SessionCommitDeadlineExceeded as exc:
            return self._finish_expired_session_commit(item, exc)
        except Exception as exc:
            self._mark_session_commit_retry(item, exc)
            return False
        return self._mark_session_commit_applied(
            item["event_id"], item["lease_owner"],
        )

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

    def reconcile_sessions_once(self, *, limit: int = AUX_LOOP_BATCH_SIZE) -> int:
        """§10.4.1 的 prepared session reconciler：先完成 Redis CAS 再转 pending。"""
        items = self._claim_session_commits(limit=limit)
        for item in items:
            try:
                with user_lock(item["userid"], timeout=5) as lease:
                    if not lease:
                        if (
                            lease.unavailable
                            and item.get("deadline_reached", False)
                        ):
                            self._mark_session_commit_terminal(
                                item,
                                error_code="session_commit_deadline",
                                error=RuntimeError(
                                    "durable session commit deadline reached while "
                                    "Redis user lock was unavailable"
                                ),
                            )
                        else:
                            self._mark_session_commit_retry(
                                item,
                                RuntimeError(
                                    "user lock unavailable"
                                    if lease.unavailable
                                    else "user lock busy"
                                ),
                            )
                        continue
                    lease.assert_owned()
                    self._apply_session_commit_item(item)
            except UserLockUnavailable as exc:
                if item.get("deadline_reached", False):
                    self._mark_session_commit_terminal(
                        item,
                        error_code="session_commit_deadline",
                        error=exc,
                    )
                else:
                    self._mark_session_commit_retry(item, exc)
            except Exception as exc:
                self._mark_session_commit_retry(item, exc)
        return len(items)

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
            if reply.recommendation_request:
                # §7.5: zero-result, legacy and off-mode searches still produce a
                # request fact even though no candidate was written into the
                # reply, so they get the facts without a delivery/outbox row.
                from app.services.recommendation_delivery_service import (
                    persist_request_fact_only,
                )
                persist_request_fact_only(
                    db,
                    inbound_event_id=int(inbound_event_id),
                    reply_index=index,
                    userid=reply.userid,
                    request_fact=reply.recommendation_request.model_dump(mode="json"),
                    source_inbound_msg_id=reply.recommendation_request.source_inbound_msg_id,
                )
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
        """认领到期 outbox；推荐目标始终先于对应 outbox 加锁。

        候选发现不加锁，每条候选随后在独立短事务中重新校验 readiness。
        推荐候选先按稳定顺序锁住全部 Job/Resume，之后才以 ``SKIP
        LOCKED`` 锁 outbox。这保留 claim 的二次复核/租约语义，并避免与
        ``Resume -> TargetCleanupTask -> outbox`` 清理链形成反向锁边。
        """
        if not self._recover_stale_outbox_claims():
            return []

        discovery = SessionLocal()
        try:
            candidates = (
                _build_outbox_claim_query(
                    discovery,
                    self._outbox_due_predicate(func.now(6)),
                    inbound_event_id=inbound_event_id,
                    limit=limit,
                    lock_rows=False,
                )
                .with_entities(
                    WecomOutboundOutbox.id,
                    WecomOutboundOutbox.recommendation_delivery_id,
                    WecomOutboundOutbox.contact_delivery_id,
                )
                .all()
            )
            discovery.rollback()
        except Exception:
            discovery.rollback()
            logger.exception("worker: discover outbox candidates failed")
            return []
        finally:
            discovery.close()

        claimed: list[dict] = []
        for candidate in candidates:
            item = self._claim_outbox_candidate(
                int(candidate[0]),
                candidate[1],
                discovered_contact_delivery_id=candidate[2],
                inbound_event_id=inbound_event_id,
            )
            if item is not None:
                claimed.append(item)
        return claimed

    @staticmethod
    def _outbox_due_predicate(now: Any):
        stale_before = func.timestampadd(
            text("SECOND"), -OUTBOX_SENDING_STALE_SECONDS, now,
        )
        return or_(
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

    def _recover_stale_outbox_claims(self) -> bool:
        """在独立事务收敛陈旧 lease，不把这些锁带入目标复核。"""
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
                # outbox 自己的枚举里 dead_letter 是合法终态（phase8 DDL），保持不变。
                stale.status = "dead_letter"
                stale.locked_at = None
                stale.last_error = "ambiguous provider outcome; automatic resend disabled"
                delivery = db.get(
                    RecommendationDelivery, stale.recommendation_delivery_id,
                )
                if delivery and delivery.status == DELIVERY_SENDING:
                    # §10.4：sending lease 过期写 unknown，且不自动重发推荐。
                    delivery.status = DELIVERY_UNKNOWN
                    delivery.last_error = stale.last_error
                    delivery.last_error_code = "sending_lease_expired"
            stale_contacts = db.query(WecomOutboundOutbox).filter(
                WecomOutboundOutbox.status == "sending",
                WecomOutboundOutbox.locked_at <= stale_before,
                WecomOutboundOutbox.contact_delivery_id.isnot(None),
            ).with_for_update(skip_locked=True).all()
            for stale in stale_contacts:
                stale.status = "pending"
                stale.locked_at = None
                stale.next_attempt_at = now
                stale.last_error = "contact delivery lease expired; retrying same delivery"
                delivery = db.get(ContactDelivery, stale.contact_delivery_id)
                if delivery and delivery.status == "sending":
                    delivery.status = "retry_wait"
            # outbox 行可能因为历史 SET NULL 或人工干预而丢失，仍然要保证 sending
            # 租约过期的 delivery 收敛到 unknown，而不是永远挂在 sending。
            db.query(RecommendationDelivery).filter(
                RecommendationDelivery.status == DELIVERY_SENDING,
                RecommendationDelivery.lease_expires_at.isnot(None),
                RecommendationDelivery.lease_expires_at <= now,
            ).update({
                "status": DELIVERY_UNKNOWN,
                "last_error": "sending lease expired; automatic resend disabled",
                "last_error_code": "sending_lease_expired",
            }, synchronize_session=False)
            db.commit()
            return True
        except Exception:
            db.rollback()
            logger.exception("worker: recover stale outbox claims failed")
            return False
        finally:
            db.close()

    def _claim_outbox_candidate(
        self,
        outbox_id: int,
        discovered_delivery_id: str | None,
        *,
        discovered_contact_delivery_id: str | None = None,
        inbound_event_id: Any = None,
    ) -> dict | None:
        db = SessionLocal()
        try:
            # The discovery snapshot is never trusted for the state transition;
            # it only determines which target locks must precede the outbox lock.
            references: list[tuple[str, int]] | None = []
            if discovered_delivery_id:
                delivery = db.get(RecommendationDelivery, discovered_delivery_id)
                if delivery is None:
                    references = None
                else:
                    references, _, _ = _recommendation_target_references(
                        copy.deepcopy(delivery.recommendation_context),
                    )
                if references is not None:
                    for target_type, target_id in references:
                        model = Job if target_type == "job" else Resume
                        (
                            db.query(model.id)
                            .filter(model.id == target_id)
                            .with_for_update()
                            .first()
                        )

            now = func.now(6)
            row = (
                _build_outbox_claim_query(
                    db,
                    self._outbox_due_predicate(now),
                    inbound_event_id=inbound_event_id,
                    outbox_id=outbox_id,
                    limit=1,
                    check_inbound_done=False,
                )
                .populate_existing()
                .first()
            )
            if row is None:
                db.rollback()
                return None
            # Validate the source event without extending the outbox lock
            # scope to the inbound row.  A consistent read remains available
            # while the dispatcher/worker owns that row's lock.
            inbound_done = db.query(WecomInboundEvent.id).filter(
                WecomInboundEvent.id == row.inbound_event_id,
                WecomInboundEvent.status == "done",
            ).first()
            if inbound_done is None:
                db.rollback()
                return None
            if (
                row.recommendation_delivery_id != discovered_delivery_id
                or row.contact_delivery_id != discovered_contact_delivery_id
            ):
                db.rollback()
                return None
            if row.recommendation_delivery_id and row.contact_delivery_id:
                row.status = "dead_letter"
                row.locked_at = None
                row.last_error = "outbox delivery kind conflict"
                db.commit()
                return None

            content = row.content
            if row.recommendation_delivery_id:
                content = self._claim_recommendation_body(db, row, now)
                if content is None:
                    db.commit()
                    return None
            elif row.contact_delivery_id:
                from app.listing.contact import (
                    CONTACT_PLATFORM_REQUEST_MESSAGE,
                )

                contact_delivery = (
                    db.query(ContactDelivery)
                    .populate_existing()
                    .filter(
                        ContactDelivery.delivery_id == row.contact_delivery_id,
                    )
                    .with_for_update()
                    .first()
                )
                if contact_delivery is None:
                    row.status = "dead_letter"
                    row.locked_at = None
                    row.last_error = "contact delivery missing"
                    db.commit()
                    return None
                if contact_delivery.status in {"sent", "revoked", "expired"}:
                    row.status = "dead_letter"
                    row.locked_at = None
                    row.last_error = (
                        f"contact delivery not sendable: {contact_delivery.status}"
                    )
                    db.commit()
                    return None
                if contact_delivery.expires_at <= datetime.now(timezone.utc).replace(tzinfo=None):
                    contact_delivery.status = "expired"
                    row.status = "dead_letter"
                    row.locked_at = None
                    row.last_error = "contact delivery expired"
                    db.commit()
                    return None
                if contact_delivery.channel == "platform_request":
                    content = CONTACT_PLATFORM_REQUEST_MESSAGE
                elif not contact_delivery.content_ciphertext:
                    row.status = "dead_letter"
                    row.locked_at = None
                    row.last_error = "contact delivery payload unavailable"
                    db.commit()
                    return None
                else:
                    row.status = "dead_letter"
                    row.locked_at = None
                    row.last_error = "unsupported contact delivery channel"
                    db.commit()
                    return None
                contact_delivery.status = "sending"
                contact_delivery.revoked_at = None
                row.status = "sending"
                row.locked_at = now
                row.attempt_count = int(row.attempt_count or 0) + 1
            else:
                row.status = "sending"
                row.locked_at = now
                row.attempt_count = int(row.attempt_count or 0) + 1
            item = {
                "id": int(row.id),
                "userid": row.userid,
                "content": content or "",
                "recommendation_delivery_id": row.recommendation_delivery_id,
                "contact_delivery_id": row.contact_delivery_id,
                "attempt_count": int(row.attempt_count),
            }
            db.commit()
            return item
        except Exception:
            db.rollback()
            logger.exception("worker: claim outbox candidate failed")
            return None
        finally:
            db.close()

    def _claim_recommendation_body(
        self, db: Session, row: WecomOutboundOutbox, now: Any,
    ) -> str | None:
        """把一条推荐 outbox 行连同它的 delivery 一起 claim 成 sending。

        返回待发送正文；返回 None 表示本轮不发送（已在函数内落好状态）。
        状态转移全部收敛到 §9.6 行 1921 的枚举，并用条件 UPDATE 防止旧 Worker
        覆盖新状态（§10.4）。
        """
        snapshot = db.get(RecommendationDelivery, row.recommendation_delivery_id)
        if snapshot is None:
            self._terminalize_recommendation_claim(
                row,
                None,
                error_code="delivery_missing",
                reason="recommendation delivery row missing",
            )
            return None

        snapshot_context = copy.deepcopy(snapshot.recommendation_context)
        references, context_error_code, context_error = (
            _recommendation_target_references(snapshot_context)
        )

        # A malformed context has no trustworthy target set to lock.  Lock the
        # delivery itself, verify that the malformed snapshot is still current,
        # then make both durable rows permanently unsendable in this transaction.
        if references is None:
            locked_delivery = (
                db.query(RecommendationDelivery)
                .populate_existing()
                .filter(
                    RecommendationDelivery.delivery_id
                    == row.recommendation_delivery_id,
                )
                .with_for_update()
                .first()
            )
            delivery = (
                locked_delivery
                if isinstance(locked_delivery, RecommendationDelivery)
                else snapshot
            )
            if isinstance(locked_delivery, RecommendationDelivery) and (
                delivery.recommendation_context != snapshot_context
            ):
                self._defer_outbox_row(
                    row, "recommendation delivery changed during claim",
                )
                return None
            self._terminalize_recommendation_claim(
                row,
                delivery,
                error_code=context_error_code or "context_invalid",
                reason=context_error or "recommendation context is invalid",
            )
            return None

        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        target_error: tuple[str, str] | None = None
        for target_type, target_id in references:
            model = Job if target_type == "job" else Resume if target_type == "resume" else None
            target = (
                db.query(model)
                .populate_existing()
                .filter(model.id == target_id)
                .with_for_update()
                .first()
            )
            valid = bool(
                target
                and target.audit_status == "passed"
                and target.activated_at is not None
                and target.candidate_expires_at is None
                and target.deleted_at is None
                and target.delist_reason is None
                and target.expires_at is not None
                and target.expires_at > now_utc
            )
            if not valid and target_error is None:
                target_error = (
                    "recommendation_target_stale",
                    "recommendation target is missing"
                    if target is None
                    else "recommendation target is no longer active",
                )

        locked_delivery = (
            db.query(RecommendationDelivery)
            .populate_existing()
            .filter(
                RecommendationDelivery.delivery_id
                == row.recommendation_delivery_id,
            )
            .with_for_update()
            .first()
        )
        delivery = locked_delivery if isinstance(locked_delivery, RecommendationDelivery) else snapshot
        if delivery is None or (
            isinstance(locked_delivery, RecommendationDelivery)
            and delivery.recommendation_context != snapshot_context
        ):
            self._defer_outbox_row(row, "recommendation delivery changed during claim")
            return None

        if target_error is not None:
            self._terminalize_recommendation_claim(
                row,
                delivery,
                error_code=target_error[0],
                reason=target_error[1],
            )
            return None

        if delivery.status == DELIVERY_PREPARED:
            # session CAS 还没完成，由独立 reconciler 负责推进；这里只推迟重扫，
            # 不能把还能恢复的投递意图打成终态。
            self._defer_outbox_row(row, "recommendation delivery still prepared")
            return None

        if delivery.status not in DELIVERY_SENDABLE_STATUSES:
            # sent/unknown/permanent_failed 都是 terminal，不再重发（§10.2/§10.3）。
            row.status = "dead_letter"
            row.locked_at = None
            row.next_attempt_at = None
            row.last_error = (
                f"recommendation delivery not sendable: {delivery.status}"
            )
            return None

        if not delivery.content_ciphertext:
            # 正文已被 TTL 清理，永远无法再发送。
            self._terminalize_recommendation_claim(
                row,
                delivery,
                error_code="content_unavailable",
                reason="recommendation delivery body unavailable",
            )
            return None

        try:
            from app.services.recommendation_delivery_service import (
                decrypt_delivery_body,
            )
            content = decrypt_delivery_body(delivery)
        except Exception as exc:
            # P2-8/§10.6：解密失败 fail-closed，保持可恢复状态并告警，
            # 既不终态化也不允许明文旁路。
            attempts = int(delivery.attempt_count or 0) + 1
            delivery.attempt_count = attempts
            delivery.status = DELIVERY_RETRY_WAIT
            delivery.last_error = f"recommendation decrypt failed: {type(exc).__name__}"
            delivery.last_error_code = "content_decrypt_failed"
            delivery.next_attempt_at = func.timestampadd(
                text("SECOND"), _delivery_backoff_seconds(attempts), func.now(6),
            )
            self._defer_outbox_row(
                row, delivery.last_error, attempts=attempts,
            )
            log_event(
                "recommendation_delivery_decrypt_failed",
                delivery_id=delivery.delivery_id,
                attempts=attempts,
                error_type=type(exc).__name__,
                severity="alert",
            )
            return None

        attempts = int(delivery.attempt_count or 0) + 1
        # §10.4 的条件 UPDATE：只有把 pending/retry_wait 改成 sending 成功的
        # Worker 才可以调用企微。
        updated = db.query(RecommendationDelivery).filter(
            RecommendationDelivery.delivery_id == delivery.delivery_id,
            RecommendationDelivery.status.in_(DELIVERY_SENDABLE_STATUSES),
            RecommendationDelivery.next_attempt_at <= func.now(6),
        ).update({
            "status": DELIVERY_SENDING,
            "attempt_count": attempts,
            "lease_owner": self._lease_owner,
            "lease_expires_at": func.timestampadd(
                text("SECOND"), OUTBOX_SENDING_STALE_SECONDS, func.now(6),
            ),
        }, synchronize_session=False)
        if updated != 1:
            self._defer_outbox_row(row, "recommendation delivery claimed elsewhere")
            return None

        row.status = "sending"
        row.locked_at = now
        row.attempt_count = int(row.attempt_count or 0) + 1
        return content

    @staticmethod
    def _terminalize_recommendation_claim(
        row: WecomOutboundOutbox,
        delivery: RecommendationDelivery | None,
        *,
        error_code: str,
        reason: str,
    ) -> None:
        """Persist an unrecoverable claim failure on both sides of the outbox."""
        outbox_reason = reason[:1000]
        row.status = "dead_letter"
        row.locked_at = None
        row.next_attempt_at = None
        row.last_error = outbox_reason

        delivery_id = row.recommendation_delivery_id
        if delivery is not None:
            delivery_id = delivery.delivery_id
            # A sending row has crossed the irreversible claim boundary, so its
            # provider outcome is unknown.  All earlier active states are safe
            # to stop permanently; existing terminal facts remain untouched.
            if delivery.status in DELIVERY_ACTIVE_STATUSES:
                terminal_status = (
                    DELIVERY_UNKNOWN
                    if delivery.status == DELIVERY_SENDING
                    else DELIVERY_PERMANENT_FAILED
                )
                delivery.status = terminal_status
                delivery.last_error = reason[:500]
                delivery.last_error_code = error_code[:32]
                delivery.lease_owner = None
                delivery.lease_expires_at = None
                delivery.session_patch_ciphertext = None

                from app.core.time_utils import to_naive_utc, utc_now
                from app.services.recommendation_delivery_service import (
                    content_expires_at_for_status,
                )
                failed_at = utc_now()
                created_at = getattr(delivery, "created_at", None)
                if not isinstance(created_at, datetime):
                    created_at = failed_at
                delivery.content_expires_at = to_naive_utc(
                    content_expires_at_for_status(
                        terminal_status,
                        created_at=created_at,
                        terminal_at=failed_at,
                    ),
                )

        log_event(
            "recommendation_delivery_claim_rejected",
            delivery_id=delivery_id,
            error_code=error_code,
            severity="alert",
        )

    @staticmethod
    def _defer_outbox_row(
        row: WecomOutboundOutbox, reason: str, *, attempts: int | None = None,
    ) -> None:
        """让一条仍可恢复的 outbox 行退避后重扫，不改变它的 pending 语义。"""
        backoff = _delivery_backoff_seconds(
            attempts if attempts is not None else int(row.attempt_count or 0) + 1,
        )
        row.status = "pending"
        row.locked_at = None
        row.next_attempt_at = func.timestampadd(
            text("SECOND"), backoff, func.now(6),
        )
        row.last_error = reason[:1000]

    def _persist_after_send(self, label: str, fn: Callable[[Session], bool]) -> bool:
        """企微已接受消息后的落库，带进程内有限重试（P2-9 / §10.3 行 2339）。

        数据库暂时不可用时只在进程内退避重试并告警，绝不重新调用企微；重试耗尽
        后保留 sending，由 lease 过期收敛到 unknown，人工按 msgid 核对。
        """
        for attempt in range(1, SEND_COMMIT_MAX_ATTEMPTS + 1):
            db = SessionLocal()
            try:
                result = fn(db)
                db.commit()
                return result
            except Exception:
                db.rollback()
                logger.exception(
                    "worker: %s persist attempt %d failed", label, attempt,
                )
            finally:
                db.close()
            if attempt < SEND_COMMIT_MAX_ATTEMPTS:
                time.sleep(
                    SEND_COMMIT_BACKOFFS[
                        min(attempt - 1, len(SEND_COMMIT_BACKOFFS) - 1)
                    ],
                )
        log_event(
            "wecom_send_commit_failed",
            label=label,
            attempts=SEND_COMMIT_MAX_ATTEMPTS,
            severity="alert",
        )
        return False

    def _mark_outbox_sent(self, outbox_id: int, provider_msg_id: str | None) -> bool:
        def _persist(db: Session) -> bool:
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
            return updated == 1

        return self._persist_after_send(f"outbox:{outbox_id}", _persist)

    def _mark_delivery_sent(self, item: dict, response: Any) -> bool:
        """持久化推荐投递的 sent 事实（§10.2 行 2306-2312）。

        白名单响应、msgid 和 invalid/unlicensed 名单在同一个事务里写入；sent 是
        曝光的唯一真源，因此提交成功后才通过正规 claim 流程触发派生（§10.5）。
        """
        from app.core.time_utils import to_naive_utc, utc_now
        from app.services.recommendation_delivery_service import (
            content_expires_at_for_status,
        )

        delivery_id = item["recommendation_delivery_id"]
        msgid = response.get("msgid") if isinstance(response, dict) else None
        whitelisted = whitelist_send_response(response)
        rejected = parse_invalid_recipients(response)
        sent_at = utc_now()
        # §9.11：sent 之后正文最多再留 24 小时；口径统一走 delivery service，
        # 不在 Worker 里另写一份分钟数。
        content_expires_at = to_naive_utc(
            content_expires_at_for_status(
                DELIVERY_SENT, created_at=sent_at, terminal_at=sent_at,
            ),
        )

        def _persist(db: Session) -> bool:
            updated = db.query(WecomOutboundOutbox).filter(
                WecomOutboundOutbox.id == item["id"],
                WecomOutboundOutbox.status == "sending",
            ).update({
                "status": "sent",
                "provider_msg_id": (str(msgid or ""))[:128] or None,
                "sent_at": func.now(6),
                "locked_at": None,
                "next_attempt_at": None,
                "last_error": None,
            })
            # 条件 UPDATE + lease owner：租约过期后被别人接管的旧 Worker
            # 不能把新状态覆盖回 sent（§10.4）。
            delivery_updated = db.query(RecommendationDelivery).filter(
                RecommendationDelivery.delivery_id == delivery_id,
                RecommendationDelivery.status == DELIVERY_SENDING,
                RecommendationDelivery.lease_owner == self._lease_owner,
            ).update({
                "status": DELIVERY_SENT,
                "wecom_msgid": (str(msgid or ""))[:128] or None,
                "wecom_response": whitelisted or None,
                "invalid_recipients": rejected or None,
                "last_error": None,
                "last_error_code": None,
                "sent_at": to_naive_utc(sent_at),
                "lease_owner": None,
                "lease_expires_at": None,
                "next_attempt_at": func.now(6),
                "content_expires_at": content_expires_at,
                # §9.11 行 2110：session patch 不进入留存。
                "session_patch_ciphertext": null(),
            }, synchronize_session=False)
            return updated == 1 and delivery_updated == 1

        ok = self._persist_after_send(f"delivery:{delivery_id}", _persist)
        if ok:
            self._submit_immediate_impressions()
        return ok

    def _mark_contact_delivery_sent(self, item: dict, response: Any) -> bool:
        """Commit a ContactDelivery and its opaque outbox row as sent."""
        delivery_id = item.get("contact_delivery_id")
        if not delivery_id:
            return False
        msgid = response.get("msgid") if isinstance(response, dict) else None

        def _persist(db: Session) -> bool:
            outbox_updated = db.query(WecomOutboundOutbox).filter(
                WecomOutboundOutbox.id == item["id"],
                WecomOutboundOutbox.status == "sending",
                WecomOutboundOutbox.contact_delivery_id == delivery_id,
            ).update({
                "status": "sent",
                "provider_msg_id": (str(msgid or ""))[:128] or None,
                "sent_at": func.now(6),
                "locked_at": None,
                "next_attempt_at": None,
                "last_error": None,
            }, synchronize_session=False)
            delivery_updated = db.query(ContactDelivery).filter(
                ContactDelivery.delivery_id == delivery_id,
                ContactDelivery.status == "sending",
            ).update({
                "status": "sent",
                "sent_at": func.now(6),
            }, synchronize_session=False)
            return outbox_updated == 1 and delivery_updated == 1

        return self._persist_after_send(f"contact_delivery:{delivery_id}", _persist)

    def _record_delivery_response(
        self,
        db: Session,
        delivery_id: str,
        response: Any,
        values: dict,
    ) -> None:
        """把白名单响应和部分失败名单并入一次 delivery 状态转移。"""
        whitelisted = whitelist_send_response(response)
        rejected = parse_invalid_recipients(response)
        if whitelisted:
            values["wecom_response"] = whitelisted
        if rejected:
            values["invalid_recipients"] = rejected
        msgid = response.get("msgid") if isinstance(response, dict) else None
        if msgid:
            values["wecom_msgid"] = str(msgid)[:128]
        db.query(RecommendationDelivery).filter(
            RecommendationDelivery.delivery_id == delivery_id,
            RecommendationDelivery.status == DELIVERY_SENDING,
            RecommendationDelivery.lease_owner == self._lease_owner,
        ).update(values, synchronize_session=False)

    def _mark_outbox_failed(
        self,
        item: dict,
        error: Exception,
        *,
        terminal: bool = False,
        response: Any = None,
        error_code: str | None = None,
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
                backoff = _delivery_backoff_seconds(attempts)
                values["next_attempt_at"] = func.timestampadd(
                    text("SECOND"), backoff, func.now(6),
                )
            db.query(WecomOutboundOutbox).filter(
                WecomOutboundOutbox.id == item["id"],
                WecomOutboundOutbox.status == "sending",
            ).update(values)
            delivery_id = item.get("recommendation_delivery_id")
            if delivery_id:
                # §10.4：可重试错误写 retry_wait/next_attempt_at/attempt_count；
                # 永久错误或用户无效写 permanent_failed。attempt_count 已在 claim
                # 时递增，这里不再重复累加。
                delivery_values: dict = {
                    "status": (
                        DELIVERY_PERMANENT_FAILED if dead else DELIVERY_RETRY_WAIT
                    ),
                    "last_error": values["last_error"],
                    "last_error_code": error_code or _error_code(error),
                    "lease_owner": None,
                    "lease_expires_at": None,
                }
                if dead:
                    # §9.11：permanent_failed 之后正文最多再留 24 小时。
                    from app.core.time_utils import to_naive_utc, utc_now
                    from app.services.recommendation_delivery_service import (
                        content_expires_at_for_status,
                    )
                    failed_at = utc_now()
                    delivery_values["content_expires_at"] = to_naive_utc(
                        content_expires_at_for_status(
                            DELIVERY_PERMANENT_FAILED,
                            created_at=failed_at,
                            terminal_at=failed_at,
                        ),
                    )
                else:
                    delivery_values["next_attempt_at"] = func.timestampadd(
                        text("SECOND"),
                        _delivery_backoff_seconds(attempts),
                        func.now(6),
                    )
                self._record_delivery_response(
                    db, delivery_id, response, delivery_values,
                )
            contact_delivery_id = item.get("contact_delivery_id")
            if contact_delivery_id:
                db.query(ContactDelivery).filter(
                    ContactDelivery.delivery_id == contact_delivery_id,
                    ContactDelivery.status == "sending",
                ).update({
                    "status": "revoked" if dead else "retry_wait",
                    "revoke_reason": error_code if dead and terminal else None,
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

        # P1-12/§10.2：企微对部分失败仍然返回 errcode=0。单用户发送时接收者出现在
        # invaliduser/unlicenseduser 里就说明消息没有下发，不能标 sent，
        # 否则会派生出一批假曝光。
        rejected_by = recipient_rejected(response, item["userid"])
        if rejected_by:
            self._mark_user_inactive(item["userid"])
            self._mark_outbox_failed(
                item,
                WeComError(
                    f"recipient rejected by wecom: {rejected_by}", errcode=0,
                ),
                terminal=True,
                response=response,
                error_code=rejected_by,
            )
            log_event(
                "wecom_send_recipient_rejected",
                user_hash=identifier_hash(item["userid"]),
                reject_field=rejected_by,
                delivery_id=item.get("recommendation_delivery_id"),
                severity="alert",
            )
            return False

        if item.get("recommendation_delivery_id"):
            return self._mark_delivery_sent(item, response)
        if item.get("contact_delivery_id"):
            return self._mark_contact_delivery_sent(item, response)
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

    def dispatch_deliveries_once(self, *, limit: int = AUX_LOOP_BATCH_SIZE) -> int:
        """§10.4.1 的 delivery dispatcher 单次扫描，只碰 DB，不读 incoming 队列。"""
        claimed = self._claim_outbox(limit=limit)
        for item in claimed:
            self._deliver_outbox_item(item)
        return len(claimed)

    def _submit_immediate_impressions(self) -> None:
        """§10.5：sent 提交后立即投递到有界 executor，用户锁释放前最多等 200ms。

        executor 每项独立 Session，并且必须经过 `claim_impression_deliveries`，
        否则会和后台 deriver 对同一条 delivery 并发派生。
        """
        executor = self._impression_executor
        if executor is None:
            # 独立 deriver 线程未启动（例如单测直接构造 Worker）；后台扫描仍然是
            # 兜底真源，这里不做内联派生以免绕过 claim 的并发保护。
            return
        try:
            future = executor.submit(
                self.derive_impressions_once,
                limit=IMPRESSION_IMMEDIATE_BATCH_SIZE,
            )
        except RuntimeError:
            # executor 已在关停中，交给下次 deriver 扫描。
            return
        try:
            future.result(timeout=IMPRESSION_IMMEDIATE_WAIT_SECONDS)
        except Exception:
            # 超时或失败都不影响 sent 事实，deriver 每 250ms 继续恢复。
            pass

    def derive_impressions_once(self, *, limit: int = AUX_LOOP_BATCH_SIZE) -> int:
        """§10.5 的曝光派生：claim → 短事务派生 → 释放派生租约。"""
        from app.services.recommendation_exposure_service import (
            claim_impression_deliveries,
            derive_impressions,
            mark_impression_retry,
        )
        claim_db = SessionLocal()
        try:
            delivery_ids = claim_impression_deliveries(claim_db, limit=limit)
            claim_db.commit()
        except Exception:
            claim_db.rollback()
            logger.exception("worker: claim impression deliveries failed")
            delivery_ids = []
        finally:
            claim_db.close()
        for delivery_id in delivery_ids:
            db = SessionLocal()
            try:
                delivery = db.get(RecommendationDelivery, delivery_id)
                if delivery:
                    # 只释放 impression_lease_*（在 derive_impressions 内完成）；
                    # 发送租约 lease_owner/lease_expires_at 属于 dispatcher，
                    # 派生侧既不读也不写。
                    derive_impressions(db, delivery)
                    db.commit()
            except Exception as exc:
                db.rollback()
                mark_impression_retry(db, delivery_id, exc)
                db.commit()
            finally:
                db.close()
        return len(delivery_ids)

    def _send_replies(self, replies: list[ReplyMessage]) -> bool:
        all_ok = True
        for reply in replies:
            ok = self._send_one(reply)
            if not ok:
                all_ok = False
        return all_ok

    def _send_one(self, reply: ReplyMessage) -> bool:
        try:
            response = self._wecom_client.send_text(reply.userid, reply.content)
            # 非推荐回复同样不能把部分失败当成功：errcode=0 时接收者仍可能落在
            # invaliduser/unlicenseduser 里（§10.2）。
            rejected_by = recipient_rejected(response, reply.userid)
            if rejected_by:
                logger.warning(
                    "worker: recipient rejected user_hash=%s field=%s",
                    identifier_hash(reply.userid), rejected_by,
                )
                self._mark_user_inactive(reply.userid)
                return False
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
                "dispatcher_lease_owner": None,
                "dispatcher_lease_expires_at": None,
            })
        except Exception:
            logger.exception("worker: mark_event_processing failed id=%s", event_id)

    def _release_dispatcher_lease(self, event_id: Any) -> None:
        """Release a failed enqueue claim so the next scan can retry promptly."""
        if not event_id:
            return
        db = SessionLocal()
        try:
            db.query(WecomInboundEvent).filter(
                WecomInboundEvent.id == event_id,
                WecomInboundEvent.dispatcher_lease_owner == self._lease_owner,
            ).update({
                "dispatcher_lease_owner": None,
                "dispatcher_lease_expires_at": None,
            })
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("worker: release dispatcher lease failed id=%s", event_id)
        finally:
            db.close()

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
            _promote_prepared_deliveries(db, event_id)
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

def _delivery_backoff_seconds(attempt_count: int) -> int:
    """§10.4 的 retry_wait 退避；与 outbox 复用同一张退避表。"""
    index = min(max(int(attempt_count or 1), 1) - 1, len(OUTBOX_RETRY_BACKOFFS) - 1)
    return OUTBOX_RETRY_BACKOFFS[index]


def _error_code(error: Exception) -> str:
    """`last_error_code` 只保存错误码或异常类型，不写脱敏前的错误正文。"""
    errcode = getattr(error, "errcode", None)
    if errcode:
        return str(errcode)[:32]
    return type(error).__name__[:32]


def _promote_prepared_deliveries(db: Session, event_id: Any) -> None:
    """Redis session CAS 落地后把本轮 delivery 从 prepared 推进到 pending。

    §9.11 行 2110：`session_patch_ciphertext` 在 prepared→pending 时立即清空，
    不进入 90 天留存。两条 UPDATE 都是条件更新且幂等，重复恢复不会回退状态。
    """
    if not event_id:
        return
    delivery_ids = db.query(
        WecomOutboundOutbox.recommendation_delivery_id,
    ).filter(
        WecomOutboundOutbox.inbound_event_id == event_id,
        WecomOutboundOutbox.recommendation_delivery_id.isnot(None),
    )
    db.query(RecommendationDelivery).filter(
        RecommendationDelivery.delivery_id.in_(delivery_ids),
        RecommendationDelivery.status == DELIVERY_PREPARED,
    ).update({
        "status": DELIVERY_PENDING,
        "next_attempt_at": func.now(6),
    }, synchronize_session=False)
    db.query(RecommendationDelivery).filter(
        RecommendationDelivery.delivery_id.in_(delivery_ids),
        RecommendationDelivery.session_commit_state != "applied",
    ).update({
        "session_commit_state": "applied",
        "session_committed_at": func.now(6),
        "session_patch_ciphertext": null(),
    }, synchronize_session=False)


def _decrypt_session_patch(delivery: RecommendationDelivery) -> dict | None:
    """解密 staged session patch；失败必须冒泡，由调用方 fail-closed（§10.6）。

    envelope/AAD/key ring 全部由 `recommendation_delivery_service` 负责，Worker 侧
    不复制一份密钥逻辑。
    """
    from app.services.recommendation_delivery_service import (
        decrypt_delivery_session_patch,
    )

    raw = decrypt_delivery_session_patch(delivery)
    if raw is None:
        return None
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("session patch payload is not a JSON object")
    return payload


def _build_wecom_message(msg_data: dict) -> WeComMessage:
    return WeComMessage(
        schema_version=int(msg_data.get("schema_version") or 1),
        msg_id=msg_data.get("msg_id") or "",
        from_user=msg_data.get("from_userid") or "",
        to_user="",
        msg_type=msg_data.get("msg_type") or "",
        content=msg_data.get("content") or "",
        media_id=msg_data.get("media_id") or "",
        create_time=int(msg_data.get("create_time") or 0),
        turn_id=msg_data.get("turn_id") or "",
        source_channel=msg_data.get("source_channel") or "wecom_app",
        conversation_type=msg_data.get("conversation_type") or "single",
        conversation_id=msg_data.get("conversation_id") or msg_data.get("from_userid") or "",
        chat_id=msg_data.get("chat_id") or "",
        ordering_key=msg_data.get("ordering_key") or "",
        provider_msg_id=msg_data.get("provider_msg_id") or "",
        provider_req_id=msg_data.get("provider_req_id") or "",
        aibot_id=msg_data.get("aibot_id") or "",
        media_storage_ref=msg_data.get("media_storage_ref") or "",
        actor_id_kind=msg_data.get("actor_id_kind") or "plain",
    )


def _inbound_event_to_queue_msg(row: WecomInboundEvent) -> dict:
    """把 wecom_inbound_event 行重建成入队消息 JSON。

    P0-2 修复要点：
    - msg_type 直接使用 row.msg_type（DB 枚举已覆盖全部 WeCom 类型）
    - media_id 来自独立列（image/voice/video/file 类型，Worker 恢复时可补下载）
    - content 只对 text/event 类型有意义；媒体类型 content_brief 只是类型标签，
      不要把它当 text content 传给 router（否则 message_router 会当成用户在发字面文本）
    """
    def _text(value: object, default: str = "") -> str:
        return value if isinstance(value, str) else default

    def _integer(value: object, default: int = 0) -> int:
        return int(value) if isinstance(value, (int, float)) else default

    raw_type = _text(row.msg_type, "text")
    content = _text(row.content_brief)
    if raw_type in ("image", "voice", "video", "file"):
        # 媒体消息：content_brief 是 "[image] media_id saved" 之类占位，
        # 业务链路不应该把它当正文
        content = ""

    return {
        "schema_version": 2,
        "inbound_event_id": _integer(row.id),
        "msg_id": _text(row.msg_id),
        "provider_msg_id": _text(getattr(row, "provider_msg_id", None)) or None,
        "turn_id": _text(getattr(row, "turn_id", None)) or None,
        "source_channel": _text(getattr(row, "source_channel", None), "wecom_app"),
        "from_userid": _text(row.from_userid),
        "conversation_type": _text(getattr(row, "conversation_type", None), "single"),
        "conversation_id": _text(getattr(row, "conversation_id", None)) or _text(row.from_userid),
        "chat_id": _text(getattr(row, "chat_id", None)) or None,
        "ordering_key": _text(getattr(row, "ordering_key", None)),
        "msg_type": raw_type,
        "content": content,
        "media_id": _text(row.media_id),
        "media_storage_ref": _text(getattr(row, "media_storage_ref", None)),
        "provider_req_id": _text(getattr(row, "provider_req_id", None)) or None,
        "aibot_id": _text(getattr(row, "aibot_id", None)) or None,
        "created_at_epoch": int(row.created_at.timestamp()) if hasattr(row.created_at, "timestamp") else 0,
        "create_time": int(row.created_at.timestamp()) if hasattr(row.created_at, "timestamp") else 0,
        "_retry_count": _integer(row.retry_count),
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
