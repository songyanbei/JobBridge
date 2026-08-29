"""企微 Webhook 入口（Phase 4）。

严格处理顺序（方案 §12.5 + phase4-main §3.1）：
    验签 → 解密 → 解析 → 幂等检查 → 限流检查 → 写 inbound_event → 入队 → 返回 200

设计约束：
- 绝对不同步调用 message_router / service / LLM
- 端到端响应 < 100ms
- 被限流消息不写入 wecom_inbound_event（不消耗存储）
- 限流参数从 system_config 读取（带内存缓存）
- 解密失败仍返回 200（避免企微重试），只返回 403 给签名失败
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi import status as http_status
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.core.logging_setup import identifier_hash
from app.core.redis_client import (
    QUEUE_INCOMING,
    QUEUE_RATE_LIMIT_NOTIFY,
    check_msg_duplicate,
    check_rate_limit,
    enqueue_message,
    get_cached_config,
    get_redis,
    set_cached_config,
)
from app.db import SessionLocal
from app.models import SystemConfig, WecomInboundEvent
from app.wecom.callback import WeComMessage, extract_encrypt_from_xml, parse_message
from app.wecom.crypto import decrypt_message, verify_signature

logger = logging.getLogger(__name__)

router = APIRouter(tags=["wecom-webhook"])

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 允许写入 wecom_inbound_event.msg_type 的 ENUM 值
# 一期仅对 text/image/voice/event 走业务路径；
# video/file/link/location 落库为"原始类型"用于审计与恢复，
# 任何未识别的类型映射为 "other"。
_VALID_INBOUND_TYPES = frozenset({
    "text", "image", "voice", "event",
    "video", "file", "link", "location",
})

# 被限流提示的去重窗口，避免同一用户在限流风暴下被重复 push
_RATE_LIMIT_NOTIFY_DEDUP_SECONDS = 60

# webhook 热路径的配置缓存 TTL（作为 Redis config_cache 未命中时的回源保护）
_CONFIG_CACHE_TTL = 60
_LOCAL_CONFIG_FALLBACK: dict[str, int] = {
    "rate_limit.window_seconds": 10,
    "rate_limit.max_count": 5,
}
_LOCAL_CONFIG_LOCK = threading.Lock()
_CONFIG_CACHE_UNAVAILABLE_UNTIL = 0.0
_CONFIG_CACHE_RETRY_SECONDS = 5.0
_RATE_LIMIT_RULE = "rate_limit.v1"
_LOCAL_RATE_LIMIT_STATE: dict[str, list[float]] = {}
_LOCAL_RATE_LIMIT_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# GET /webhook/wecom —— URL 验证
# ---------------------------------------------------------------------------

@router.get("/webhook/wecom")
def verify_callback(
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    echostr: str = Query(...),
) -> Response:
    """企微回调 URL 校验。

    企微在配置回调 URL 时，会发送一次 GET 请求。需要：
    1. 校验签名
    2. 解密 echostr，返回明文
    """
    if not verify_signature(
        token=settings.wecom_token,
        timestamp=timestamp,
        nonce=nonce,
        encrypt=echostr,
        msg_signature=msg_signature,
    ):
        logger.warning("webhook: GET signature verify failed")
        raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN)

    try:
        plaintext = decrypt_message(
            aes_key_base64=settings.wecom_aes_key,
            encrypt=echostr,
            corp_id=settings.wecom_corp_id,
        )
    except ValueError as exc:
        logger.error("webhook: GET decrypt failed: %s", exc)
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST)

    return Response(content=plaintext, media_type="text/plain")


# ---------------------------------------------------------------------------
# POST /webhook/wecom —— 回调消息推送
# ---------------------------------------------------------------------------

@router.post("/webhook/wecom")
async def receive_callback(
    request: Request,
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
) -> Response:
    """企微回调消息推送。必须快速返回 200。"""
    start_ts = time.monotonic()
    body_bytes = await request.body()
    body_text = body_bytes.decode("utf-8", errors="replace")

    # 1. 提取 Encrypt
    try:
        encrypt = extract_encrypt_from_xml(body_text)
    except ValueError as exc:
        logger.error("webhook: invalid callback XML: %s", exc)
        return _success_response()

    # 2. 验签
    if not verify_signature(
        token=settings.wecom_token,
        timestamp=timestamp,
        nonce=nonce,
        encrypt=encrypt,
        msg_signature=msg_signature,
    ):
        logger.warning("webhook: POST signature verify failed")
        raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN)

    # 3. 解密
    try:
        plaintext = decrypt_message(
            aes_key_base64=settings.wecom_aes_key,
            encrypt=encrypt,
            corp_id=settings.wecom_corp_id,
        )
    except ValueError as exc:
        # 解密失败返回 200 避免企微重试
        logger.error("webhook: decrypt failed: %s", exc)
        return _success_response()

    # 4. 解析
    try:
        msg = parse_message(plaintext)
    except Exception as exc:
        logger.exception("webhook: parse_message failed: %s", exc)
        return _success_response()

    # Redis and SQLAlchemy clients are synchronous. Keep them off the asyncio
    # event loop so concurrent callbacks can still be decrypted/accepted while
    # an infrastructure operation is waiting on its bounded timeout.
    return await run_in_threadpool(_accept_message, msg, start_ts)


def _accept_message(msg: WeComMessage, start_ts: float) -> Response:
    """Durably accept and enqueue one parsed callback on a worker thread."""
    if not msg.msg_id:
        # event 类型等没有 MsgId，直接忽略，不入队不记录
        logger.info("webhook: skipping msg without msg_id, type=%s", msg.msg_type)
        return _success_response()

    # 5. 幂等检查（L1 Redis）
    try:
        if check_msg_duplicate(msg.msg_id):
            # SETNX is only a cache hint, not the durable acceptance record. A
            # previous attempt may have set the key and then failed to write DB.
            # Skipping solely on Redis would acknowledge and permanently lose
            # every subsequent WeCom retry for that message.
            if _inbound_event_exists(msg.msg_id):
                logger.info("webhook: duplicate msg_id=%s, skip", msg.msg_id)
                return _success_response()
            logger.warning(
                "webhook: stale L1 dedup marker without durable event msg_id=%s",
                msg.msg_id,
            )
    except Exception:
        # Redis 不可用 → 靠下面 inbound_event UNIQUE(msg_id) 兜底
        logger.exception("webhook: check_msg_duplicate failed (degraded to L2)")

    # 6. Durable inbox first.  The event is intentionally accepted before the
    # rate-limit decision so a rejected message remains auditable and replayable.
    if not msg.from_user:
        logger.warning("webhook: msg without from_user, msg_id=%s", msg.msg_id)
        return _success_response()

    inbound_event_id = _insert_inbound_event(msg)
    if inbound_event_id is None:
        logger.error(
            "webhook: durable acceptance failed, request upstream retry msg_id=%s",
            msg.msg_id,
        )
        return Response(
            content="temporary failure",
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            media_type="text/plain",
        )
    if getattr(msg, "_durable_existing", False):
        # A Redis outage can race a duplicate DB insert. Reuse the original
        # event and do not run the limiter or emit a second notice.
        return _success_response()

    # 7. 限流检查（窗口参数从 system_config 读，带缓存）
    window, max_count = _get_rate_limit_params()
    try:
        allowed = check_rate_limit(msg.from_user, window=window, max_count=max_count)
    except Exception:
        logger.warning("webhook: check_rate_limit failed, use local conservative limiter", exc_info=True)
        allowed = _local_rate_limit_allow(msg.from_user, window=window, max_count=max_count)

    if not allowed:
        logger.info(
            "webhook: rate-limited user_hash=%s",
            identifier_hash(msg.from_user),
        )
        if not _mark_rate_limited(inbound_event_id, rule=_RATE_LIMIT_RULE):
            return Response(
                content="temporary failure",
                status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
                media_type="text/plain",
            )
        _async_rate_limit_notify(msg.from_user)
        return _success_response()

    # 8. 入队
    queue_msg = {
        "msg_id": msg.msg_id,
        "turn_id": getattr(msg, "turn_id", ""),
        "from_userid": msg.from_user,
        "msg_type": msg.msg_type,
        "content": msg.content,
        "media_id": msg.media_id,
        "create_time": msg.create_time,
        "inbound_event_id": inbound_event_id,
        # Worker 用于计算真实排队时延；重试时保留原值，反映端到端积压。
        "_enqueued_at": time.time(),
    }
    try:
        enqueue_message(json.dumps(queue_msg, ensure_ascii=False), QUEUE_INCOMING)
    except Exception:
        # 入队失败不影响返回 200：Worker 启动自检会根据 inbound_event.status=received 恢复
        logger.exception("webhook: enqueue failed, will rely on inbound_event recovery")

    elapsed_ms = (time.monotonic() - start_ts) * 1000
    logger.info(
        "webhook: accepted msg_id=%s user_hash=%s type=%s elapsed_ms=%.1f",
        msg.msg_id, identifier_hash(msg.from_user), msg.msg_type, elapsed_ms,
    )
    return _success_response()


# ---------------------------------------------------------------------------
# 内部：写 wecom_inbound_event
# ---------------------------------------------------------------------------

def _insert_inbound_event(msg: WeComMessage) -> int | None:
    """写入 wecom_inbound_event。失败不阻塞入队，返回主键（失败时 None）。

    - 原始 msg_type 保留（枚举已扩展到 text/image/voice/video/file/link/location/event/other）
    - media_id 独立落列（image/voice/video/file）以支持 Worker crash 后补下载
    - content_brief 只承担文本摘要，不再兼作 media_id 存储
    """
    enum_type = msg.msg_type if msg.msg_type in _VALID_INBOUND_TYPES else "other"
    brief = _build_brief(msg)
    media_id = msg.media_id or None

    db = SessionLocal()
    try:
        event = WecomInboundEvent(
            msg_id=msg.msg_id,
            turn_id=str(uuid.uuid4()),
            from_userid=msg.from_user or "",
            msg_type=enum_type,
            media_id=media_id,
            content_brief=brief,
            status="received",
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        msg.turn_id = event.turn_id
        return event.id
    except IntegrityError:
        # UNIQUE(msg_id) 撞库 → 幂等兜底（L2）
        db.rollback()
        logger.info("webhook: inbound_event duplicate msg_id=%s (L2 idempotency)", msg.msg_id)
        existing = db.query(WecomInboundEvent).filter(
            WecomInboundEvent.msg_id == msg.msg_id,
        ).first()
        if existing:
            msg.turn_id = existing.turn_id or ""
            msg._durable_existing = True
        return existing.id if existing else None
    except Exception:
        db.rollback()
        logger.exception("webhook: insert inbound_event failed msg_id=%s", msg.msg_id)
        return None
    finally:
        db.close()


def _mark_rate_limited(event_id: int, *, rule: str) -> bool:
    """Record a terminal, auditable rate-limit decision."""
    db = SessionLocal()
    try:
        updated = db.query(WecomInboundEvent).filter(
            WecomInboundEvent.id == int(event_id),
            WecomInboundEvent.status == "received",
            WecomInboundEvent.rate_limit_decision == "accepted",
        ).update({
            "status": "done",
            "rate_limit_decision": "rate_limited",
            "rate_limit_rule": rule,
            "rate_limited_at": func.now(6),
            "worker_finished_at": func.now(6),
        })
        db.commit()
        return updated == 1
    except Exception:
        db.rollback()
        logger.exception("webhook: mark rate-limited event failed id=%s", event_id)
        return False
    finally:
        db.close()


def _local_rate_limit_allow(userid: str, *, window: int, max_count: int) -> bool:
    """Bounded process-local limiter used only while Redis is unavailable."""
    now = time.monotonic()
    cutoff = now - max(1, int(window))
    with _LOCAL_RATE_LIMIT_LOCK:
        recent = [ts for ts in _LOCAL_RATE_LIMIT_STATE.get(userid, []) if ts > cutoff]
        allowed = len(recent) < max(1, int(max_count))
        recent.append(now)
        _LOCAL_RATE_LIMIT_STATE[userid] = recent[-max(1, int(max_count)) :]
        return allowed


def _inbound_event_exists(msg_id: str) -> bool:
    """Return whether Redis's L1 dedup marker has a durable DB counterpart."""
    db = SessionLocal()
    try:
        return db.query(WecomInboundEvent.id).filter(
            WecomInboundEvent.msg_id == msg_id,
        ).first() is not None
    except Exception:
        # On an uncertain DB read, continue to the INSERT path. Its UNIQUE key
        # remains the authoritative L2 idempotency check.
        logger.exception(
            "webhook: durable dedup lookup failed msg_id=%s", msg_id,
        )
        return False
    finally:
        db.close()


def _build_brief(msg: WeComMessage) -> str:
    """生成 content_brief：文本截断前 500；媒体类型仅做类型提示，实际 media_id 走独立列。"""
    if msg.msg_type == "text":
        text = msg.content or ""
        return text[:500]
    if msg.msg_type in ("image", "voice", "video", "file"):
        # media_id 已落到独立列；这里只做审计/排查的人类可读摘要
        return f"[{msg.msg_type}] media_id saved"
    # event / link / location / other
    raw_type = msg.msg_type or "unknown"
    content = msg.content or ""
    return f"[{raw_type}] {content[:480]}"


# ---------------------------------------------------------------------------
# 限流参数读取（带缓存）
# ---------------------------------------------------------------------------

def _get_rate_limit_params() -> tuple[int, int]:
    window = _get_config_int("rate_limit.window_seconds", 10)
    max_count = _get_config_int("rate_limit.max_count", 5)
    return window, max_count


def _get_config_int(key: str, default: int) -> int:
    """读取 int 配置。

    使用 Redis `config_cache:{key}` 做唯一缓存层，由 `system_config_service.update`
    更新配置后主动 invalidate。避免进程内缓存导致的多实例配置不一致与变更不生效。
    """
    global _CONFIG_CACHE_UNAVAILABLE_UNTIL
    with _LOCAL_CONFIG_LOCK:
        if time.monotonic() < _CONFIG_CACHE_UNAVAILABLE_UNTIL:
            return _LOCAL_CONFIG_FALLBACK.get(key, default)

    try:
        cached = get_cached_config(key)
    except Exception:
        # Redis outage is exactly when opening a fresh synchronous DB connection
        # on every callback causes an event-loop/thread-pool collapse. Use the
        # process's last known value (or the safe default) until Redis recovers.
        logger.warning(
            "webhook: config cache unavailable, use local fallback key=%s", key,
            exc_info=True,
        )
        with _LOCAL_CONFIG_LOCK:
            _CONFIG_CACHE_UNAVAILABLE_UNTIL = (
                time.monotonic() + _CONFIG_CACHE_RETRY_SECONDS
            )
            return _LOCAL_CONFIG_FALLBACK.get(key, default)

    with _LOCAL_CONFIG_LOCK:
        _CONFIG_CACHE_UNAVAILABLE_UNTIL = 0.0

    if cached is not None:
        try:
            value = int(cached)
            with _LOCAL_CONFIG_LOCK:
                _LOCAL_CONFIG_FALLBACK[key] = value
            return value
        except (ValueError, TypeError):
            # 缓存异常值，继续回源 DB
            pass

    with _LOCAL_CONFIG_LOCK:
        value = _LOCAL_CONFIG_FALLBACK.get(key, default)
    db = SessionLocal()
    try:
        cfg = db.query(SystemConfig).filter(SystemConfig.config_key == key).first()
        if cfg:
            try:
                value = int(cfg.config_value)
            except (ValueError, TypeError):
                logger.warning(
                    "webhook: invalid integer config key=%s value=%r",
                    key,
                    cfg.config_value,
                )
    except Exception:
        logger.exception("webhook: read system_config %s failed (use default)", key)
    finally:
        db.close()

    with _LOCAL_CONFIG_LOCK:
        _LOCAL_CONFIG_FALLBACK[key] = value

    # 回填 Redis 缓存供下次命中；失败时本地 last-known 仍可继续服务。
    try:
        set_cached_config(key, str(value), ttl=_CONFIG_CACHE_TTL)
    except Exception:
        logger.warning(
            "webhook: config cache backfill failed key=%s", key,
            exc_info=True,
        )
    return value


# ---------------------------------------------------------------------------
# 被限流的异步回复
# ---------------------------------------------------------------------------

def _async_rate_limit_notify(userid: str) -> None:
    """被限流时 push 到专用的 best-effort 通知队列。

    与 queue:send_retry 隔离的原因：
    - 限流提示是"即发即弃"：发失败就算了，不应退避重试（防止限流风暴下积压）
    - 同一用户 60s 内只 push 一次（SETNX 去重），避免限流循环堆 N 倍提示
    - 独立队列便于运维监控限流告警量
    """
    # 文案延迟 import，避免 webhook 加载时拉起 message_router 依赖链
    from app.services.message_router import RATE_LIMITED_REPLY

    try:
        r = get_redis()
        # 60s 去重：同一用户在窗口内只 push 一次限流提示
        dedup_key = f"rate_limit_notified:{userid}"
        first = r.set(dedup_key, "1", nx=True, ex=_RATE_LIMIT_NOTIFY_DEDUP_SECONDS)
        if not first:
            return
        payload = {
            "userid": userid,
            "content": RATE_LIMITED_REPLY,
            "source": "rate_limit_notify",
        }
        r.rpush(QUEUE_RATE_LIMIT_NOTIFY, json.dumps(payload, ensure_ascii=False))
    except Exception:
        logger.exception("webhook: enqueue rate-limit notify failed")


def _success_response() -> Response:
    return Response(content="success", media_type="text/plain")
