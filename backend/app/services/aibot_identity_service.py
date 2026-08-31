"""Worker-side AIBot actor resolution and fail-closed policy."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy.orm import Session

from app.config import settings
from app.models import AibotIdentityAudit, WecomAibotIdentity
from app.services.registration_service import actor_digest, auto_register_worker, ensure_binding
from app.services import aibot_identity_metrics
from app.wecom.identity_client import ConversionResult, IdentityClientError, WeComIdentityAppClient

_CANONICAL_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")


@dataclass(frozen=True)
class ResolvedActor:
    actor_id: str
    actor_id_kind: str
    status: str
    canonical_userid: str | None = None
    reason_code: str | None = None
    binding_id: str | None = None

    @property
    def verified(self) -> bool:
        return self.status == "verified" and bool(self.canonical_userid)


class AibotIdentityService:
    def __init__(self, client: WeComIdentityAppClient | None = None, *, plain_verifier: Callable[[str], bool] | None = None, bot_id: str | None = None):
        self.client = client
        self.plain_verifier = plain_verifier or (lambda _userid: True)
        self.bot_id = bot_id or settings.wecom_aibot_bot_id

    def observe_actor(self, db: Session, actor_id: str, *, actor_id_kind: str = "open_userid", bot_id: str | None = None, source_msg_id: str | None = None) -> WecomAibotIdentity:
        if not isinstance(actor_id, str) or not actor_id or len(actor_id) > 128:
            raise ValueError("invalid actor id")
        digest = actor_digest(actor_id)
        bot = bot_id or self.bot_id or ""
        row = db.query(WecomAibotIdentity).filter(WecomAibotIdentity.opaque_actor_id == actor_id).first()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if row is None:
            row = WecomAibotIdentity(
                opaque_actor_id=actor_id, bot_id=bot,
                actor_id_kind="plain" if actor_id_kind == "plain" else "open_userid",
                opaque_actor_digest=digest, identity_status="unverified",
                source_msg_id=source_msg_id, first_seen_at=now, last_seen_at=now,
            )
            db.add(row)
        else:
            row.last_seen_at = now
            if source_msg_id:
                row.source_msg_id = source_msg_id
        db.flush()
        return row

    def verify_plain_userid(self, userid: str) -> tuple[bool, str]:
        if not isinstance(userid, str) or not _CANONICAL_RE.fullmatch(userid):
            return False, "invalid_canonical_userid"
        try:
            if not self.plain_verifier(userid):
                return False, "directory_not_visible"
        except Exception:
            return False, "directory_lookup_failed"
        return True, "verified"

    def resolve_open_userids(self, userids: list[str] | tuple[str, ...] | set[str]) -> ConversionResult:
        if self.client is None:
            raise IdentityClientError("identity client is not configured", code="identity_client_missing", retryable=True)
        return self.client.batch_openuserid_to_userid(userids)

    def resolve_for_event(self, db: Session, *, actor_id: str, actor_id_kind: str = "open_userid", bot_id: str | None = None, source_msg_id: str | None = None, auto_register: bool = True) -> ResolvedActor:
        row = self.observe_actor(db, actor_id, actor_id_kind=actor_id_kind, bot_id=bot_id, source_msg_id=source_msg_id)
        aibot_identity_metrics.record_identity_seen(actor_id_kind)
        bot = bot_id or self.bot_id or row.bot_id or ""
        digest = row.opaque_actor_digest or actor_digest(actor_id)
        if actor_id_kind == "plain":
            ok, reason = self.verify_plain_userid(actor_id)
            if not ok:
                row.identity_status = "rejected"
                row.last_error_code = reason
                self._audit(db, bot, digest, actor_id, "plain_verify", "rejected", reason)
                aibot_identity_metrics.record_resolution("rejected", reason)
                db.flush()
                return ResolvedActor(actor_id, actor_id_kind, "rejected", reason_code=reason)
            canonical = actor_id
        else:
            # Existing, explicitly verified mappings remain usable while the
            # resolver kill switch is off; the switch only prevents new
            # network resolution/registration.
            if row.identity_status == "verified" and (row.canonical_userid or row.mapped_external_userid):
                canonical = row.canonical_userid or row.mapped_external_userid
            elif not settings.identity_resolution_enabled:
                return ResolvedActor(actor_id, actor_id_kind, "unverified", reason_code="identity_resolution_disabled")
            else:
                canonical = None
            if canonical is not None:
                pass
            else:
                try:
                    result = self.resolve_open_userids([actor_id])
                except IdentityClientError as exc:
                    row.identity_status = "conversion_pending" if exc.retryable else "rejected"
                    row.resolution_attempts = int(row.resolution_attempts or 0) + 1
                    row.last_error_code = exc.code
                    row.next_resolution_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=min(3600, 2 ** min(row.resolution_attempts, 10))) if exc.retryable else None
                    self._audit(db, bot, digest, actor_id, "openuserid_conversion", "pending" if exc.retryable else "rejected", exc.code)
                db.flush()
                aibot_identity_metrics.record_resolution(row.identity_status, exc.code)
                return ResolvedActor(actor_id, actor_id_kind, row.identity_status, reason_code=exc.code)
                if actor_id in result.invalid or actor_id not in result.mapping:
                    row.identity_status = "rejected"
                    row.last_error_code = "invalid_open_userid"
                    self._audit(db, bot, digest, actor_id, "openuserid_conversion", "rejected", "invalid_open_userid")
                db.flush()
                aibot_identity_metrics.record_resolution("rejected", "invalid_open_userid")
                return ResolvedActor(actor_id, actor_id_kind, "rejected", reason_code="invalid_open_userid")
                canonical = result.mapping[actor_id]
        row.mapped_external_userid = canonical
        row.canonical_userid = canonical
        row.identity_status = "verified"
        row.verified_at = datetime.now(timezone.utc).replace(tzinfo=None)
        row.last_error_code = None
        binding = ensure_binding(db, bot_id=bot, opaque_actor_digest_value=digest, canonical_userid=canonical)
        if auto_register:
            auto_register_worker(db, canonical, binding)
        self._audit(db, bot, digest, actor_id, "identity_verified", "verified", canonical=canonical)
        db.flush()
        aibot_identity_metrics.record_resolution("verified", "")
        return ResolvedActor(actor_id, actor_id_kind, "verified", canonical_userid=canonical, binding_id=binding.binding_id)

    @staticmethod
    def _audit(db: Session, bot: str, digest: str, actor: str, action: str, result: str, reason: str | None = None, canonical: str | None = None) -> None:
        db.add(AibotIdentityAudit(bot_id=bot, opaque_actor_digest=digest, canonical_userid=canonical, action=action, result=result, reason_code=reason, actor=hashlib.sha256(actor.encode()).hexdigest()[:16]))


IdentityService = AibotIdentityService
