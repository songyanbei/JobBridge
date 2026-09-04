"""Durable, PII-free parse artifacts shared by Action Gateway retries."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ActionParseArtifact
from app.services import demo_scope

PARSE_SCHEMA_VERSION = "action-gateway.v1"
_SENSITIVE_KEYS = frozenset({
    "phone", "wechat", "wechat_id", "contact", "contact_person", "mobile", "email",
})


def _safe_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _safe_payload(item) for key, item in value.items() if str(key).lower() not in _SENSITIVE_KEYS}
    if isinstance(value, list):
        return [_safe_payload(item) for item in value]
    return value


def parse_digest(payload: dict[str, Any], session_hint_digest: str, classifier_version: str, schema_version: str = PARSE_SCHEMA_VERSION) -> str:
    canonical = json.dumps(
        {"parse": _safe_payload(payload), "session_hint_digest": session_hint_digest, "classifier_version": classifier_version, "schema_version": schema_version},
        ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def persist_parse_artifact(
    db: Session, *, parse_ref: str, turn_id: str, actor_userid: str,
    payload: dict[str, Any], parse_digest_value: str, classifier_version: str,
    session_version: int | None = None, schema_version: str = PARSE_SCHEMA_VERSION,
    expires_at: datetime | None = None, retention_seconds: int = 86400,
) -> ActionParseArtifact:
    """Insert/read an artifact without committing the caller transaction."""
    if not parse_ref or not turn_id or not actor_userid:
        raise ValueError("parse_ref, turn_id and actor_userid are required")
    safe = _safe_payload(payload)
    if safe != payload:
        raise ValueError("parse_payload_contains_sensitive_field")
    if not isinstance(payload, dict):
        raise ValueError("parse_payload_must_be_object")
    if not parse_digest_value or len(parse_digest_value) != 64:
        raise ValueError("invalid_parse_digest")
    expiry = expires_at or (datetime.now(timezone.utc) + timedelta(seconds=retention_seconds))
    if expiry.tzinfo is not None:
        expiry = expiry.astimezone(timezone.utc).replace(tzinfo=None)
    row = ActionParseArtifact(
        demo_id=demo_scope.demo_id_or_none(),
        parse_ref=parse_ref, turn_id=turn_id, actor_userid=actor_userid,
        parse_digest=parse_digest_value, schema_version=schema_version,
        classifier_version=classifier_version, session_version=session_version,
        payload=payload, expires_at=expiry,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
        demo_scope.register(db, "action_parse_artifact", row.parse_ref)
    except IntegrityError:
        row = read_parse_artifact(
            db, parse_ref, turn_id=turn_id, actor_userid=actor_userid,
            parse_digest_value=parse_digest_value, schema_version=schema_version,
        )
        if row is None:
            raise
    return row


def read_parse_artifact(
    db: Session, parse_ref: str, *, turn_id: str, actor_userid: str,
    parse_digest_value: str | None = None, schema_version: str = PARSE_SCHEMA_VERSION,
    now: datetime | None = None,
) -> ActionParseArtifact | None:
    """Read only a bound, unexpired artifact; mismatches fail closed."""
    row = db.query(ActionParseArtifact).filter(ActionParseArtifact.parse_ref == parse_ref).one_or_none()
    if row is None:
        return None
    if row.turn_id != turn_id or row.actor_userid != actor_userid:
        raise ValueError("parse_artifact_binding_mismatch")
    if row.schema_version != schema_version:
        raise ValueError("parse_artifact_schema_mismatch")
    if parse_digest_value is not None and row.parse_digest != parse_digest_value:
        raise ValueError("parse_artifact_digest_mismatch")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is not None:
        current = current.astimezone(timezone.utc).replace(tzinfo=None)
    if row.expires_at <= current:
        return None
    if _safe_payload(row.payload) != row.payload:
        raise ValueError("parse_artifact_contains_sensitive_field")
    return row


__all__ = ["PARSE_SCHEMA_VERSION", "parse_digest", "persist_parse_artifact", "read_parse_artifact"]
