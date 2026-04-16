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
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi import status as http_status
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.core.redis_client import (
    QUEUE_INCOMING,
    QUEUE_RATE_LIMIT_NOTIFY,
    check_msg_duplicate,
    check_rate_limit,
    enqueue_message,
    get_redis,
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

# 系统配置内存缓存：key -> (value, expires_at_ts)
_CONFIG_CACHE_TTL = 60  # 秒
_config_cache: dict[str, tuple[int, float]] = {}


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

    if not msg.msg_id:
        # event 类型等没有 MsgId，直接忽略，不入队不记录
        logger.info("webhook: skipping msg without msg_id, type=%s", msg.msg_type)
        return _success_response()

    # 5. 幂等检查（L1 Redis）
    try:
        if check_msg_duplicate(msg.msg_id):
            logger.info("webhook: duplicate msg_id=%s, skip", msg.msg_id)
            return _success_response()
    except Exception:
        # Redis 不可用 → 靠下面 inbound_event UNIQUE(msg_id) 兜底
        logger.exception("webhook: check_msg_duplicate failed (degraded to L2)")

    # 6. 限流检查（窗口参数从 system_config 读，带缓存）
    if not msg.from_user:
        logger.warning("webhook: msg without from_user, msg_id=%s", msg.msg_id)
        return _success_response()

    window, max_count = _get_rate_limit_params()
    try:
        allowed = check_rate_limit(msg.from_user, window=window, max_count=max_count)
    except Exception:
        logger.exception("webhook: check_rate_limit failed (fail-open)")
        allowed = True

    if not allowed:
        logger.info("webhook: rate-limited userid=%s", msg.from_user)
        _async_rate_limit_notify(msg.from_user)
        # 被限流的消息不写 inbound_event，不入队
        return _success_response()

    # 7. 写 wecom_inbound_event
    inbound_event_id = _insert_inbound_event(msg)

    # 8. 入队
    queue_msg = {
        "msg_id": msg.msg_id,
        "from_userid": msg.from_user,
        "msg_type": msg.msg_type,
        "content": msg.content,
        "media_id": msg.media_id,
        "create_time": msg.create_time,
        "inbound_event_id": inbound_event_id,
    }
    try:
        enqueue_message(json.dumps(queue_msg, ensure_ascii=False), QUEUE_INCOMING)
    except Exception:
        # 入队失败不影响返回 200：Worker 启动自检会根据 inbound_event.status=received 恢复
        logger.exception("webhook: enqueue failed, will rely on inbound_event recovery")

    elapsed_ms = (time.monotonic() - start_ts) * 1000
    logger.info(
        "webhook: accepted msg_id=%s userid=%s type=%s elapsed_ms=%.1f",
        msg.msg_id, msg.from_user, msg.msg_type, elapsed_ms,
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
            from_userid=msg.from_user or "",
            msg_type=enum_type,
            media_id=media_id,
            content_brief=brief,
            status="received",
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        return event.id
    except IntegrityError:
        # UNIQUE(msg_id) 撞库 → 幂等兜底（L2）
        db.rollback()
        logger.info("webhook: inbound_event duplicate msg_id=%s (L2 idempotency)", msg.msg_id)
        existing = db.query(WecomInboundEvent).filter(
            WecomInboundEvent.msg_id == msg.msg_id,
        ).first()
        return existing.id if existing else None
    except Exception:
        db.rollback()
        logger.exception("webhook: insert inbound_event failed msg_id=%s", msg.msg_id)
        return None
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
    now = time.monotonic()
    cached = _config_cache.get(key)
    if cached is not None and cached[1] > now:
        return cached[0]

    value = default
    db = SessionLocal()
    try:
        cfg = db.query(SystemConfig).filter(SystemConfig.config_key == key).first()
        if cfg:
            try:
                value = int(cfg.config_value)
            except (ValueError, TypeError):
                value = default
    except Exception:
        logger.exception("webhook: read system_config %s failed (use default)", key)
    finally:
        db.close()

    _config_cache[key] = (value, now + _CONFIG_CACHE_TTL)
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
