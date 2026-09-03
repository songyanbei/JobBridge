"""Command builders for the WeCom AI bot WebSocket protocol."""
from __future__ import annotations

import hashlib
import secrets
from typing import Any


class AibotClientError(ValueError):
    pass


def new_req_id(prefix: str = "req") -> str:
    return f"{prefix}-{secrets.token_hex(12)}"


def stable_aibot_stream_id(inbound_event_id: int | str, reply_index: int) -> str:
    """Return a deterministic stream id for one durable reply intent.

    AIBot ordinary-message replies use the stream message format.  The id must
    survive a worker retry, while different replies produced for the same
    callback must remain distinct.  Deriving it from the durable inbound row
    and reply index avoids relying on an outbox auto-increment value that is
    not available until after the row is flushed.
    """
    if not str(inbound_event_id) or int(reply_index) < 0:
        raise AibotClientError("inbound_event_id and non-negative reply_index are required")
    digest = hashlib.sha256(f"{inbound_event_id}\0{int(reply_index)}".encode()).hexdigest()
    return f"aibot-stream-{digest}"


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

    def respond_msg(
        self,
        req_id: str,
        content: str,
        *,
        stream_id: str | None = None,
        finish: bool = True,
    ) -> dict[str, Any]:
        """Build an ordinary-message reply using the official stream body.

        ``msgtype=text`` is reserved for the welcome reply command.  Ordinary
        ``aibot_respond_msg`` replies must use ``stream`` even when the result
        is sent in one final frame.
        """
        return self.stream(
            req_id,
            stream_id or stable_aibot_stream_id(req_id, 0),
            content,
            finish=finish,
        )

    def send_msg(
        self,
        req_id: str,
        content: str,
        *,
        chat_id: str | None = None,
        chat_type: int | None = None,
    ) -> dict[str, Any]:
        # Active push (aibot_send_msg) does not use the welcome-only text
        # shape. Markdown is the documented string-based push format.
        if not isinstance(content, str) or not content:
            raise AibotClientError("content is required")
        body: dict[str, Any] = {"msgtype": "markdown", "markdown": {"content": content}}
        if chat_id:
            body["chatid"] = chat_id
        if chat_type in {1, 2}:
            body["chat_type"] = chat_type
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

    def rollback_stream(self, stream_id: str, req_id: str) -> None:
        """Forget a locally-built stream when its frame was not written.

        ``stream(..., finish=True)`` records the terminal state before the
        transport writes the frame.  If the socket fails before that write,
        the durable outbox is safe to retry and the client must permit the
        same stream id to be rebuilt.
        """
        if self._streams.get(stream_id) in {req_id, "__finished__"}:
            self._streams.pop(stream_id, None)

    def respond_update_msg(
        self,
        req_id: str,
        template_card: dict[str, Any],
        *,
        userids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Build the official template-card update command.

        Stream updates intentionally use :meth:`stream` and the
        ``aibot_respond_msg`` command.  ``aibot_respond_update_msg`` is only
        valid for ``template_card_event`` callbacks.
        """
        if not isinstance(template_card, dict) or not template_card:
            raise AibotClientError("template_card is required")
        body: dict[str, Any] = {
            "response_type": "update_template_card",
            "template_card": template_card,
        }
        if userids:
            if not isinstance(userids, list) or not all(isinstance(value, str) and value for value in userids):
                raise AibotClientError("userids must be a list of non-empty strings")
            body["userids"] = userids
        return self._frame("aibot_respond_update_msg", req_id, body)

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
