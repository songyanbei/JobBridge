"""Mock 企业微信测试台 · 路由。

5 个路由，前缀 /mock/wework：

| 方法+路径              | 用途                                                       |
|------------------------|------------------------------------------------------------|
| GET  /users            | 列出 wm_mock_% 前缀的身份，供切换器下拉                    |
| GET  /oauth2/authorize | 伪 OAuth2 授权跳转（纯字段形态演示）                       |
| GET  /code2userinfo    | 伪 code → userinfo 接口（字段名与企微官方一致）            |
| POST /inbound          | 入站桥接：复用主后端 queue:incoming 链路                   |
| GET  /sse              | SSE 出站推送：订阅 mock:outbound:{external_userid}         |

所有响应顶层恒有 errcode / errmsg，字段名严格对齐企业微信官方契约。
"""
import json
import secrets
import time
from urllib.parse import urlencode

import redis
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import settings
from db import get_db
from models import MockUser, MockWecomInboundEvent
from outbound_bus import iter_frames, subscribe, unsubscribe

router = APIRouter(prefix="/mock/wework", tags=["mock-wework"])

# 主后端的 queue key，必须和 backend/app/core/redis_client.QUEUE_INCOMING 一致
_QUEUE_INCOMING = "queue:incoming"

# 主后端 wecom_inbound_event.msg_type 的合法枚举
_VALID_MSG_TYPES = {
    "text", "image", "voice", "video", "file",
    "link", "location", "event", "other",
}


def _redis() -> redis.Redis:
    """沙箱自己的 Redis 客户端（不借用主后端连接池）。"""
    return redis.Redis.from_url(settings.redis_url, decode_responses=True)


# ============================================================================
# 1) GET /users —— 身份切换器数据源
# ============================================================================

@router.get("/users")
def list_mock_users(db: Session = Depends(get_db)) -> dict:
    """返回所有 wm_mock_ 前缀的可选身份，按 role 分类由前端展示。"""
    rows = (
        db.query(MockUser)
        .filter(MockUser.external_userid.like("wm_mock_%"))
        .order_by(MockUser.role, MockUser.external_userid)
        .all()
    )
    return {
        "errcode": 0,
        "errmsg": "ok",
        "users": [
            {
                "external_userid": u.external_userid,
                "name": u.display_name or u.external_userid,
                "role": u.role,
                "avatar": "",
            }
            for u in rows
        ],
    }


# ============================================================================
# 2) GET /oauth2/authorize —— 伪 OAuth2 授权跳转
# ============================================================================

@router.get("/oauth2/authorize")
def mock_authorize(
    appid: str = Query(..., description="企业 corpid"),
    redirect_uri: str = Query(..., description="回跳地址（需 urldecode 后为合法 URL）"),
    response_type: str = Query("code"),
    scope: str = Query("snsapi_base", description="snsapi_base 或 snsapi_privateinfo"),
    agentid: str | None = Query(None),
    state: str | None = Query(None),
):
    """302 跳回 redirect_uri + code + state。不校验 appid / agentid。

    字段名和跳转格式完全对齐企微
    https://developer.work.weixin.qq.com/document/path/91022
    """
    code = f"MOCK_CODE_{secrets.token_hex(8)}"
    qs: dict[str, str] = {"code": code}
    if state:
        qs["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    target = f"{redirect_uri}{sep}{urlencode(qs)}"
    return RedirectResponse(target, status_code=302)


# ============================================================================
# 3) GET /code2userinfo —— 伪 code 换 userinfo
# ============================================================================

@router.get("/code2userinfo")
def mock_code2userinfo(
    access_token: str = Query(..., description="企微 access_token，沙箱不校验"),
    code: str = Query(..., description="授权码，沙箱不校验"),
    request: Request = None,  # type: ignore
):
    """返回体字段严格对齐 /cgi-bin/auth/getuserinfo 外部联系人分支。

    沙箱约定：前端在发起授权跳转时，通过 redirect_uri 的 query 带上
    `x_mock_external_userid=wm_mock_xxx`，回跳时原样带回；本路由读这个
    附加参数作身份源（真企微环境下此参数不存在，由 code 真实映射）。
    """
    fallback = "wm_mock_worker_001"
    external_userid = request.query_params.get("x_mock_external_userid", fallback) if request else fallback
    return {
        "errcode": 0,
        "errmsg": "ok",
        "external_userid": external_userid,
        "openid": f"mock_openid_{external_userid}",
    }


# ============================================================================
# 4) POST /inbound —— 入站桥接
# ============================================================================

@router.post("/inbound")
def mock_inbound(payload: dict, db: Session = Depends(get_db)) -> dict:
    """接收模拟用户发送的消息，复用主后端的 queue:incoming 链路。

    字段名使用企微 XML 解密后的大写驼峰（ToUserName / FromUserName / ...）。
    幂等：Redis L1（600s）+ DB L2（wecom_inbound_event.msg_id UNIQUE）。
    """
    # 1. 字段校验（按企微 XML 解密后结构）
    required = ["ToUserName", "FromUserName", "CreateTime", "MsgType", "Content", "MsgId", "AgentID"]
    missing = [k for k in required if k not in payload]
    if missing:
        return {"errcode": 40001, "errmsg": f"missing fields: {missing}"}

    msg_id = str(payload["MsgId"])
    from_userid = str(payload["FromUserName"])
    msg_type_raw = str(payload["MsgType"])
    content = str(payload["Content"])
    try:
        create_time = int(payload["CreateTime"])
    except (ValueError, TypeError):
        return {"errcode": 40002, "errmsg": "CreateTime must be int"}

    # 2. msg_type 规整（不在枚举范围内的统一落 other，和主后端一致）
    enum_type = msg_type_raw if msg_type_raw in _VALID_MSG_TYPES else "other"

    # 3. Redis L1 幂等
    r = _redis()
    try:
        if not r.set(f"wecom:msg:{msg_id}", "1", nx=True, ex=600):
            return {"errcode": 0, "errmsg": "ok (duplicate dropped)", "msgid": msg_id}
    except redis.RedisError:
        # Redis 挂了就靠 L2 兜底
        pass

    # 4. DB L2 幂等 + 写 wecom_inbound_event
    content_brief = content[:500] if content else None
    event = MockWecomInboundEvent(
        msg_id=msg_id,
        from_userid=from_userid,
        msg_type=enum_type,
        media_id=None,
        content_brief=content_brief,
        status="received",
    )
    db.add(event)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # UNIQUE(msg_id) 冲突 → 跨 Redis TTL 的重放
        return {"errcode": 0, "errmsg": "ok (duplicate in db)", "msgid": msg_id}
    db.refresh(event)

    # 5. 入队（payload 字段必须和主后端 webhook.py:193-201 完全一致）
    queue_msg = {
        "msg_id": msg_id,
        "from_userid": from_userid,
        "msg_type": enum_type,
        "content": content,
        "media_id": None,
        "create_time": create_time,
        "inbound_event_id": event.id,
    }
    try:
        r.rpush(_QUEUE_INCOMING, json.dumps(queue_msg, ensure_ascii=False))
    except redis.RedisError as exc:
        # 主后端 Worker 启动自检会按 status=received 补救
        return {
            "errcode": 0,
            "errmsg": f"ok (enqueue degraded: {exc.__class__.__name__})",
            "msgid": msg_id,
        }

    return {"errcode": 0, "errmsg": "ok", "msgid": msg_id}


# ============================================================================
# 5) GET /sse —— SSE 出站推送
# ============================================================================

@router.get("/sse")
async def mock_sse(external_userid: str = Query(..., min_length=1)):
    """SSE 订阅指定身份的出站消息 channel。

    协议：
    - 首帧 `event: ready`
    - 业务帧 `event: message` + JSON 原样 payload
    - 保活帧 `event: ping` 每 15s 一次
    """
    target_key = external_userid
    pubsub = subscribe(target_key)

    async def event_stream():
        try:
            yield f"event: ready\ndata: {{\"external_userid\":\"{external_userid}\",\"ts\":{int(time.time())}}}\n\n"
            async for frame in iter_frames(pubsub):
                yield frame
        finally:
            unsubscribe(pubsub)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
