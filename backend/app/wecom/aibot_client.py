"""Command builders for the WeCom AI bot WebSocket protocol."""
from __future__ import annotations

import secrets
from typing import Any


class AibotClientError(ValueError):
    pass


def new_req_id(prefix: str = "req") -> str:
    return f"{prefix}-{secrets.token_hex(12)}"


class AibotClient:
    """Pure protocol client; transport ownership is intentionally separate."""

    def __init__(self, bot_id: str, secret: str):
        if not bot_id or not secret:
            raise ValueError("bot_id and secret are required")
        self.bot_id = bot_id
        self._secret = secret
        self._streams: dict[str, str] = {}

    @staticmethod
    def _frame(cmd: str, req_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(req_id, str) or not req_id:
            raise AibotClientError("req_id is required")
        return {"cmd": cmd, "headers": {"req_id": req_id}, "body": body or {}}

    def subscribe(self, req_id: str | None = None) -> dict[str, Any]:
        return self._frame("aibot_subscribe", req_id or new_req_id("sub"), {"bot_id": self.bot_id, "secret": self._secret})

    def ping(self, req_id: str | None = None) -> dict[str, Any]:
        return self._frame("ping", req_id or new_req_id("ping"))

    def respond_welcome(self, req_id: str, content: str) -> dict[str, Any]:
        return self._text_frame("aibot_respond_welcome_msg", req_id, content)

    respond_welcome_msg = respond_welcome

    def respond_msg(self, req_id: str, content: str) -> dict[str, Any]:
        return self._text_frame("aibot_respond_msg", req_id, content)

    def send_msg(self, req_id: str, content: str, *, chat_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"msgtype": "text", "text": {"content": content}}
        if chat_id:
            body["chatid"] = chat_id
        return self._frame("aibot_send_msg", req_id, body)

    def _text_frame(self, cmd: str, req_id: str, content: str) -> dict[str, Any]:
        if not isinstance(content, str) or not content:
            raise AibotClientError("content is required")
        return self._frame(cmd, req_id, {"msgtype": "text", "text": {"content": content}})

    def stream(self, req_id: str, stream_id: str, content: str, *, finish: bool = False) -> dict[str, Any]:
        if not stream_id:
            raise AibotClientError("stream.id is required")
        previous_req = self._streams.get(stream_id)
        if previous_req is not None and previous_req != req_id:
            raise AibotClientError("stream must reuse its original req_id")
        if stream_id in self._streams and self._streams[stream_id] == "__finished__":
            raise AibotClientError("stream already finished")
        self._streams[stream_id] = req_id
        if finish:
            self._streams[stream_id] = "__finished__"
        return self._frame("aibot_respond_msg", req_id, {"msgtype": "stream", "stream": {"id": stream_id, "finish": bool(finish), "content": content}})

    def respond_update_msg(self, req_id: str, stream_id: str, content: str, *, finish: bool = False) -> dict[str, Any]:
        if stream_id not in self._streams or self._streams[stream_id] != req_id:
            raise AibotClientError("unknown stream or req_id mismatch")
        if self._streams[stream_id] == "__finished__":
            raise AibotClientError("stream already finished")
        if finish:
            self._streams[stream_id] = "__finished__"
        return self._frame("aibot_respond_update_msg", req_id, {"msgtype": "stream", "stream": {"id": stream_id, "finish": bool(finish), "content": content}})

    # Explicit builder aliases make the command names easy to discover for
    # callers that construct frames without retaining a client instance.
    build_subscribe = subscribe
    build_ping = ping
    build_respond_msg = respond_msg
    build_respond_update_msg = respond_update_msg
    build_send_msg = send_msg

    @staticmethod
    def parse_ack(payload: str | bytes | dict[str, Any]) -> tuple[str, int, str]:
        import json
        if not isinstance(payload, dict):
            payload = json.loads(payload.decode() if isinstance(payload, bytes) else payload)
        req_id = ((payload.get("headers") or {}).get("req_id"))
        if not req_id:
            raise AibotClientError("ack missing headers.req_id")
        return req_id, int(payload.get("errcode", -1)), str(payload.get("errmsg", ""))


AIBotClient = AibotClient
