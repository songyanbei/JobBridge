"""Pure pre-routing for Workstream A actions."""
from __future__ import annotations
import hashlib, json, uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from app.config import settings
from app.services import intent_service

SUPPORTED_ACTIONS = frozenset({
    "search_job", "show_more_job", "relax_job",
    "publish_job", "edit_job_draft", "confirm_job", "delist_job", "restore_job",
    "publish_resume", "replace_resume", "confirm_resume", "edit_resume_draft", "delist_resume", "restore_resume",
})
ACTION_GATEWAY_SCHEMA_VERSION = "action-gateway.v1"
CLASSIFIER_VERSION = "intent-adapter.v1"
_SENSITIVE_KEYS = frozenset({
    "phone", "wechat", "wechat_id", "contact", "contact_person", "mobile",
})
_ACCEPT = frozenset({"好", "好的", "可以", "行", "同意", "确认", "放宽", "是"})
_CONFIRM = frozenset({"确认发布", "发布", "确认", "确定发布"})

def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)

def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()

def _safe_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _safe_payload(v) for k, v in value.items() if str(k).lower() not in _SENSITIVE_KEYS}
    if isinstance(value, list):
        return [_safe_payload(v) for v in value]
    return value

@dataclass(frozen=True)
class ActionGatewayParse:
    parse_ref: str
    parse_digest: str
    parse_schema_version: str
    classifier_version: str
    session_version: int | None
    payload: dict[str, Any]
    intent_result: Any
    expires_at: datetime

@dataclass(frozen=True)
class ActionEnvelope:
    turn_id: str
    action_name: str
    request_digest: str
    classifier_version: str
    legacy_reason: str | None = None
    trace: dict[str, Any] | None = None
    parse_ref: str | None = None
    parse_digest: str | None = None
    parse_schema_version: str | None = None
    session_version: int | None = None
    parse_expires_at: datetime | None = None
    parse_payload: dict[str, Any] | None = None
    intent_result: Any = None

    @property
    def is_supported(self) -> bool:
        return self.action_name in SUPPORTED_ACTIONS

def _parse_from_route(route, session) -> ActionGatewayParse:
    intent_result = getattr(route, "intent_result", route)
    parsed = getattr(route, "parse_result", None)
    if parsed is not None and hasattr(parsed, "model_dump"):
        payload = _safe_payload(parsed.model_dump(mode="json"))
    elif hasattr(intent_result, "model_dump"):
        data = intent_result.model_dump(mode="json")
        payload = _safe_payload({k: data.get(k) for k in ("intent", "structured_data", "criteria_patch", "missing_fields", "confidence")})
    else:
        payload = _safe_payload({"intent": getattr(intent_result, "intent", "unknown")})
    hint = _safe_payload(intent_service.build_session_hint(session) if session is not None else {})
    digest = _digest({"parse": payload, "session_hint_digest": _digest(hint), "classifier_version": CLASSIFIER_VERSION, "schema_version": ACTION_GATEWAY_SCHEMA_VERSION})
    now = datetime.now(timezone.utc)
    return ActionGatewayParse(str(uuid.uuid4()), digest, ACTION_GATEWAY_SCHEMA_VERSION, CLASSIFIER_VERSION, getattr(session, "version", None), payload, intent_result, now + timedelta(seconds=int(getattr(settings, "action_parse_cache_ttl_seconds", 60))))

class ActionGateway:
    """Build an immutable action envelope without business side effects."""
    def __init__(self, *, mode: str | None = None):
        self.mode = mode or getattr(settings, "action_execution_mode", "off")

    def classify(self, msg, *, session=None, actor=None, turn_id: str | None = None) -> ActionEnvelope:
        turn_id = turn_id or getattr(msg, "turn_id", None) or getattr(msg, "msg_id", None) or str(uuid.uuid4())
        text = str(getattr(msg, "content", "") or "").strip()
        if self.mode == "off":
            return self._envelope(turn_id, "none", legacy_reason="mode_off")
        if not text:
            return self._envelope(turn_id, "unknown", legacy_reason="empty_text")
        # Confirmation operates on the durable draft and does not re-run the
        # intent classifier, preserving one provider call per turn.
        if getattr(session, "pending_upload_intent", None) in {"upload_job", "upload_resume"} and text in _CONFIRM:
            action_name = "confirm_resume" if getattr(session, "pending_upload_intent", None) == "upload_resume" else "confirm_job"
            digest = _digest({
                "turn_id": turn_id,
                "action_name": action_name,
                "draft": _safe_payload(getattr(session, "pending_upload", {}) or {}),
            })
            return self._envelope(turn_id, action_name, request_digest=digest)
        try:
            route = intent_service.classify_for_action_gateway(text=text, role=getattr(actor, "role", "worker"), history=getattr(session, "history", None), session=session, user_msg_id=getattr(msg, "msg_id", None), userid=getattr(msg, "from_user", None))
            parsed = _parse_from_route(route, session)
        except Exception:
            return self._envelope(turn_id, "unknown", legacy_reason="classifier_error")
        intent = str(getattr(parsed.intent_result, "intent", "unknown") or "unknown")
        pending = dict(getattr(session, "pending_relaxation", None) or {})
        if pending and text in _ACCEPT and pending.get("step"):
            action = "relax_job"
        elif intent == "show_more":
            action = "show_more_job"
        elif intent in {"search_job", "follow_up"} and getattr(actor, "role", "worker") == "worker":
            action = "search_job"
        elif intent == "upload_resume":
            action = "publish_resume"
        elif intent in {"search_worker", "upload_job", "command", "chitchat"}:
            action = "none"
        else:
            action = "unknown"
        criteria = getattr(parsed.intent_result, "structured_data", {}) or {}
        digest = _digest({"turn_id": turn_id, "action_name": action, "parse_digest": parsed.parse_digest, "criteria": _safe_payload(criteria), "profile": getattr(session, "profile", None), "action_version": "v1"})
        return self._envelope(turn_id, action, request_digest=digest, parse=parsed)

    @staticmethod
    def _envelope(turn_id, action_name, *, request_digest=None, legacy_reason=None, parse=None):
        return ActionEnvelope(turn_id, action_name, request_digest or _digest({"turn_id": turn_id, "action_name": action_name}), parse.classifier_version if parse else CLASSIFIER_VERSION, legacy_reason, {"gateway_schema": ACTION_GATEWAY_SCHEMA_VERSION}, parse.parse_ref if parse else None, parse.parse_digest if parse else None, parse.parse_schema_version if parse else None, parse.session_version if parse else None, parse.expires_at if parse else None, parse.payload if parse else None, parse.intent_result if parse else None)

__all__ = ["ActionEnvelope", "ActionGateway", "ActionGatewayParse", "SUPPORTED_ACTIONS"]
