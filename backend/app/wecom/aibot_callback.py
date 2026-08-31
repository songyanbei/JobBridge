"""Strict parser for the WeCom AI bot WebSocket JSON protocol.

The parser deliberately returns a reduced message object.  It never retains the
original payload, bot secret, media URL, or media AES key in a long-lived value.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from app.wecom.callback import WeComMessage

MAX_FRAME_BYTES = 256 * 1024
MAX_TEXT_CHARS = 20_000
MAX_PARTS = 32
MAX_ID_LENGTH = 128
ALLOWED_CALLBACK_COMMANDS = {"aibot_msg_callback", "aibot_event_callback"}
ALLOWED_CHAT_TYPES = {"single", "group"}
ALLOWED_MESSAGE_TYPES = {"text", "image", "voice", "file", "video", "mixed", "event"}


class AibotProtocolError(ValueError):
    """Raised when a frame violates the documented AIBot protocol."""


@dataclass(frozen=True)
class AibotMediaPart:
    msg_type: str
    media_id: str = ""
    media_url: str = ""
    media_aes_key: str = ""
    content: str = ""


@dataclass(frozen=True)
class AibotFrame:
    cmd: str
    req_id: str
    body: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AibotCallback:
    """Validated callback with only fields needed by durable acceptance."""

    command: str
    req_id: str
    provider_msg_id: str
    aibot_id: str
    chat_type: str
    from_user: str
    msg_type: str
    chat_id: str = ""
    content: str = ""
    create_time: int = 0
    media_id: str = ""
    media_url: str = ""
    media_aes_key: str = ""
    media_expires_at: int | None = None
    parts: tuple[AibotMediaPart, ...] = ()
    event_type: str = ""

    @property
    def conversation_type(self) -> str:
        return self.chat_type

    @property
    def conversation_id(self) -> str:
        return self.chat_id if self.chat_type == "group" else self.from_user

    @property
    def ordering_key(self) -> str:
        return f"wecom:wecom_aibot:{self.chat_type}:{self.conversation_id}"

    @property
    def session_key(self) -> str:
        return f"wecom:aibot:{self.chat_type}:{self.conversation_id}"

    @property
    def dedupe_key(self) -> str:
        return hashlib.sha256(f"wecom_aibot\0{self.provider_msg_id}".encode()).hexdigest()

    def to_wecom_message(self) -> WeComMessage:
        """Project the validated callback into the shared message DTO."""
        return WeComMessage(
            schema_version=2,
            msg_id="aibot_" + hashlib.sha256(
                f"wecom_aibot\0{self.provider_msg_id}".encode()
            ).hexdigest()[:58],
            from_user=self.from_user,
            to_user=self.aibot_id,
            msg_type=self.msg_type,
            content=self.content,
            media_id=self.media_id,
            create_time=self.create_time,
            source_channel="wecom_aibot",
            conversation_type=self.chat_type,
            conversation_id=self.conversation_id,
            chat_id=self.chat_id,
            ordering_key=self.ordering_key,
            provider_req_id=self.req_id,
            provider_msg_id=self.provider_msg_id,
            aibot_id=self.aibot_id,
            media_url=self.media_url,
            media_aes_key=self.media_aes_key,
            media_expires_at=self.media_expires_at,
            actor_id_kind="opaque",
            raw_parts=[
                {"type": p.msg_type, "length": len(p.content), "has_media": bool(p.media_id)}
                for p in self.parts
            ],
        )


def _bounded_id(value: Any, name: str, *, required: bool = True) -> str:
    if not isinstance(value, str) or (required and not value):
        raise AibotProtocolError(f"missing {name}")
    if len(value) > MAX_ID_LENGTH:
        raise AibotProtocolError(f"{name} exceeds {MAX_ID_LENGTH} characters")
    return value


def decode_frame(payload: str | bytes | bytearray | dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, (bytes, bytearray)):
        if len(payload) > MAX_FRAME_BYTES:
            raise AibotProtocolError("frame exceeds size limit")
        payload = bytes(payload).decode("utf-8")
    if not isinstance(payload, str) or len(payload.encode("utf-8")) > MAX_FRAME_BYTES:
        raise AibotProtocolError("frame exceeds size limit")
    try:
        value = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise AibotProtocolError("invalid JSON frame") from exc
    if not isinstance(value, dict):
        raise AibotProtocolError("frame must be a JSON object")
    return value


def parse_frame(payload: str | bytes | bytearray | dict[str, Any], *, expected_cmd: str | None = None) -> AibotFrame:
    value = decode_frame(payload)
    cmd = _bounded_id(value.get("cmd"), "cmd")
    if expected_cmd and cmd != expected_cmd:
        raise AibotProtocolError(f"unexpected cmd {cmd}")
    headers = value.get("headers")
    if not isinstance(headers, dict):
        raise AibotProtocolError("missing headers")
    req_id = _bounded_id(headers.get("req_id"), "headers.req_id")
    body = value.get("body", {})
    if not isinstance(body, dict):
        raise AibotProtocolError("body must be an object")
    return AibotFrame(cmd=cmd, req_id=req_id, body=body)


def _parse_parts(body: dict[str, Any], msg_type: str, chat_type: str) -> tuple[AibotMediaPart, ...]:
    if msg_type != "mixed":
        return ()
    mixed = body.get("mixed", {})
    if not isinstance(mixed, dict):
        raise AibotProtocolError("mixed must be an object")
    raw_parts = mixed.get("msg_item", [])
    if not isinstance(raw_parts, list) or len(raw_parts) > MAX_PARTS:
        raise AibotProtocolError("invalid mixed parts")
    parts: list[AibotMediaPart] = []
    for raw in raw_parts:
        if not isinstance(raw, dict):
            raise AibotProtocolError("invalid mixed part")
        part_type = _bounded_id(raw.get("msgtype"), "mixed.msgtype")
        if part_type not in ALLOWED_MESSAGE_TYPES - {"mixed", "event"}:
            raise AibotProtocolError("unsupported mixed part")
        if part_type != "text" and chat_type != "single":
            raise AibotProtocolError("media is only supported in single chat")
        part_text = raw.get("text", {}) if part_type == "text" else {}
        if part_type == "text" and not isinstance(part_text, dict):
            raise AibotProtocolError("mixed.text must be an object")
        text = part_text.get("content", "") if part_type == "text" else ""
        if not isinstance(text, str) or len(text) > MAX_TEXT_CHARS:
            raise AibotProtocolError("text exceeds size limit")
        media_id = raw.get("media_id", "") or ""
        media_url = raw.get("url", "") or ""
        media_aes_key = raw.get("aeskey", "") or ""
        if not all(isinstance(value, str) for value in (media_id, media_url, media_aes_key)):
            raise AibotProtocolError("mixed media fields must be strings")
        parts.append(AibotMediaPart(part_type, media_id, media_url, media_aes_key, text))
    return tuple(parts)


def _parse_media_expires(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise AibotProtocolError("invalid media expires_at")
    try:
        expires_at = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AibotProtocolError("invalid media expires_at") from exc
    if expires_at <= 0:
        raise AibotProtocolError("invalid media expires_at")
    return expires_at


def parse_callback(payload: str | bytes | bytearray | dict[str, Any]) -> AibotCallback:
    value = decode_frame(payload)
    cmd = value.get("cmd")
    if cmd not in ALLOWED_CALLBACK_COMMANDS:
        raise AibotProtocolError(f"unsupported callback cmd {cmd!r}")
    frame = parse_frame(value, expected_cmd=cmd)
    body = frame.body
    provider_msg_id = _bounded_id(body.get("msgid"), "body.msgid")
    aibot_id = _bounded_id(body.get("aibotid"), "body.aibotid")
    chat_type = body.get("chattype")
    if chat_type not in ALLOWED_CHAT_TYPES:
        raise AibotProtocolError("body.chattype must be single or group")
    sender = body.get("from")
    if not isinstance(sender, dict):
        raise AibotProtocolError("missing body.from")
    from_user = _bounded_id(sender.get("userid"), "body.from.userid")
    chat_id = _bounded_id(body.get("chatid"), "body.chatid", required=chat_type == "group")
    msg_type = _bounded_id(body.get("msgtype"), "body.msgtype")
    if msg_type not in ALLOWED_MESSAGE_TYPES:
        raise AibotProtocolError(f"unsupported msgtype {msg_type}")
    if msg_type != "text" and msg_type != "event" and chat_type != "single":
        raise AibotProtocolError("media is only supported in single chat")
    create_time = body.get("create_time", 0)
    try:
        create_time = int(create_time or 0)
    except (TypeError, ValueError) as exc:
        raise AibotProtocolError("invalid create_time") from exc
    text_body = body.get("text", {}) if msg_type == "text" else {}
    if msg_type == "text" and not isinstance(text_body, dict):
        raise AibotProtocolError("body.text must be an object")
    text = text_body.get("content", "") if msg_type == "text" else ""
    if not isinstance(text, str) or len(text) > MAX_TEXT_CHARS:
        raise AibotProtocolError("text exceeds size limit")
    media = body.get(msg_type, {}) if msg_type in {"image", "voice", "file", "video"} else {}
    if not isinstance(media, dict):
        raise AibotProtocolError(f"body.{msg_type} must be an object")
    parts = _parse_parts(body, msg_type, chat_type)
    if msg_type == "mixed":
        text = "".join(p.content for p in parts if p.msg_type == "text")
    event_body = body.get("event", {}) if msg_type == "event" else {}
    if msg_type == "event" and not isinstance(event_body, dict):
        raise AibotProtocolError("body.event must be an object")
    event_type = event_body.get("eventtype", "") if msg_type == "event" else ""
    if not isinstance(event_type, str):
        raise AibotProtocolError("invalid event type")
    media_id = media.get("media_id", body.get("media_id", "")) or ""
    media_url = media.get("url", "") or ""
    media_aes_key = media.get("aeskey", "") or ""
    if not all(isinstance(value, str) for value in (media_id, media_url, media_aes_key)):
        raise AibotProtocolError("media fields must be strings")
    return AibotCallback(
        command=cmd,
        req_id=frame.req_id,
        provider_msg_id=provider_msg_id,
        aibot_id=aibot_id,
        chat_type=chat_type,
        from_user=from_user,
        msg_type=msg_type,
        chat_id=chat_id,
        content=text,
        create_time=create_time,
        media_id=media_id,
        media_url=media_url,
        media_aes_key=media_aes_key,
        media_expires_at=_parse_media_expires(media.get("expires_at")),
        parts=parts,
        event_type=str(event_type or ""),
    )


parse_message = parse_callback

