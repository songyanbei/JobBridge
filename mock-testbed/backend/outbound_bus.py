"""Mock 企业微信测试台 · 出站总线。

主后端 WeComClient.send_* 被 [MOCK-WEWORK] 分支短路后，会把出站 payload
publish 到 Redis channel `mock:outbound:{target_key}`。本模块封装订阅 +
迭代成 SSE 帧的逻辑。

约定：
- 点对点消息 channel = mock:outbound:{external_userid}
- 群消息 channel   = mock:outbound:chat:{chat_id}
- SSE 每 15s 发一次 ping 保活
"""
import asyncio
import json
from typing import AsyncIterator

import redis

from config import settings

_CHANNEL_PREFIX = "mock:outbound:"
# SSE 保活：每 15 秒发一次 event: ping
_SSE_PING_INTERVAL_SECONDS = 15
# 轮询间隔：50ms → ~20Hz；配合 get_message(timeout=0) 非阻塞读取
_PUBSUB_POLL_INTERVAL_SECONDS = 0.05


def _redis() -> redis.Redis:
    return redis.Redis.from_url(settings.redis_url, decode_responses=True)


def publish(target_key: str, payload: dict) -> int:
    """发布出站 payload（仅用于沙箱内部 self-test / 测试场景）。

    生产路径：payload 由主后端 wecom/client.py 的 [MOCK-WEWORK] 分支发布。

    Returns:
        收到此消息的订阅者数量（redis PUBLISH 命令原生返回值）。
    """
    r = _redis()
    return r.publish(f"{_CHANNEL_PREFIX}{target_key}", json.dumps(payload, ensure_ascii=False))


def subscribe(target_key: str) -> redis.client.PubSub:
    """订阅某个 target_key 的出站消息。返回 pubsub 对象，调用方负责关闭。"""
    r = _redis()
    pubsub = r.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(f"{_CHANNEL_PREFIX}{target_key}")
    return pubsub


def unsubscribe(pubsub: redis.client.PubSub) -> None:
    """释放 pubsub 资源。异常吞掉（断连时别炸）。"""
    try:
        pubsub.unsubscribe()
        pubsub.close()
    except Exception:
        pass


async def iter_frames(pubsub: redis.client.PubSub) -> AsyncIterator[str]:
    """把 pubsub 消息转成 SSE 帧，_PUBSUB_POLL_INTERVAL_SECONDS 节拍轮询。

    生成的帧已按 SSE 协议拼接（含末尾双换行），直接 yield 给 StreamingResponse。

    实现注意：`get_message(timeout=0)` 非阻塞 —— 不能用 timeout>0，否则会阻塞整个
    async 事件循环（并发 SSE 连接会被串行化）；由 `await asyncio.sleep` 负责让出事件循环。
    """
    loop = asyncio.get_event_loop()
    last_ping = loop.time()
    while True:
        msg = pubsub.get_message(ignore_subscribe_messages=True, timeout=0)
        if msg and msg.get("type") == "message":
            data = msg.get("data", "")
            if isinstance(data, bytes):
                data = data.decode("utf-8", errors="replace")
            yield f"event: message\ndata: {data}\n\n"

        now = loop.time()
        if now - last_ping >= _SSE_PING_INTERVAL_SECONDS:
            yield f"event: ping\ndata: {{\"ts\":{int(now)}}}\n\n"
            last_ping = now

        await asyncio.sleep(_PUBSUB_POLL_INTERVAL_SECONDS)
