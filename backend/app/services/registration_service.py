"""AIBot registration, invitation and role-binding policy.

The functions in this module are intentionally explicit transaction helpers.
They never accept an opaque actor as a ``User`` key and never infer a role from
chat text.  Callers own the surrounding transaction/commit.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    AibotIdentityAudit,
    AibotIdentityBinding,
    AibotRegistration,
    AibotRoleInvite,
    User,
)


def token_digest(token: str) -> str:
    if not isinstance(token, str) or not token:
        raise ValueError("invite token is required")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def actor_digest(actor: str) -> str:
    key = settings.aibot_identity_digest_key.get_secret_value() or settings.app_secret_key
    return hmac.new(key.encode("utf-8"), actor.encode("utf-8"), hashlib.sha256).hexdigest()


def _audit(db: Session, *, bot_id: str, digest: str, action: str, result: str, canonical: str | None = None, reason: str | None = None, actor: str | None = None, metadata: dict | None = None) -> None:
    db.add(AibotIdentityAudit(
        bot_id=bot_id, opaque_actor_digest=digest, canonical_userid=canonical,
        action=action, result=result, reason_code=reason, actor=actor, audit_metadata=metadata,
    ))


def ensure_binding(
    db: Session,
    *,
    bot_id: str,
    opaque_actor_digest_value: str,
    canonical_userid: str,
    source: str = "auto_verified",
) -> AibotIdentityBinding:
    if not canonical_userid or len(canonical_userid) > 64:
        raise ValueError("canonical userid is required")
    existing = db.query(AibotIdentityBinding).filter(
        AibotIdentityBinding.bot_id == bot_id,
        AibotIdentityBinding.opaque_actor_digest == opaque_actor_digest_value,
        AibotIdentityBinding.binding_status == "active",
    ).first()
    if existing:
        if existing.canonical_userid != canonical_userid:
            existing.binding_status = "rejected"
            _audit(db, bot_id=bot_id, digest=opaque_actor_digest_value, action="binding_conflict", result="rejected", canonical=canonical_userid, reason="identity_already_bound")
            raise ValueError("identity is already bound to another canonical userid")
        return existing
    conflict = db.query(AibotIdentityBinding).filter(
        AibotIdentityBinding.bot_id == bot_id,
        AibotIdentityBinding.canonical_userid == canonical_userid,
        AibotIdentityBinding.binding_status == "active",
    ).first()
    if conflict and conflict.opaque_actor_digest != opaque_actor_digest_value:
        _audit(db, bot_id=bot_id, digest=opaque_actor_digest_value, action="binding_conflict", result="rejected", canonical=canonical_userid, reason="canonical_already_bound")
        raise ValueError("canonical userid is already bound to another identity")
    binding = AibotIdentityBinding(
        binding_id=str(uuid.uuid4()), bot_id=bot_id,
        opaque_actor_digest=opaque_actor_digest_value, canonical_userid=canonical_userid,
        binding_status="active", binding_source=source,
    )
    db.add(binding)
    db.flush()
    _audit(db, bot_id=bot_id, digest=opaque_actor_digest_value, action="binding_created", result="active", canonical=canonical_userid)
    return binding


def auto_register_worker(db: Session, canonical_userid: str, binding: AibotIdentityBinding) -> User:
    """Create the minimum worker account for a verified canonical member."""
    if not canonical_userid or canonical_userid != binding.canonical_userid:
        raise ValueError("canonical binding mismatch")
    user = db.query(User).filter(User.external_userid == canonical_userid).first()
    if user is None:
        user = User(
            external_userid=canonical_userid, role="worker", status="active",
            can_search_jobs=1, can_search_workers=0,
        )
        db.add(user)
        db.flush()
    registration = db.query(AibotRegistration).filter(
        AibotRegistration.identity_binding_id == binding.binding_id,
    ).first()
    if registration is None:
        db.add(AibotRegistration(
            registration_id=str(uuid.uuid4()), canonical_userid=canonical_userid,
            identity_binding_id=binding.binding_id, registration_status="active",
            registration_source="auto_worker", requested_role="worker", granted_role="worker",
            capability_snapshot={"can_search_jobs": True, "can_search_workers": False},
        ))
    elif registration.registration_status not in {"active", "revoked"}:
        registration.registration_status = "active"
        registration.granted_role = "worker"
    return user


def create_invite(db: Session, *, role: str, expires_at: datetime, operator: str, max_uses: int = 1) -> tuple[AibotRoleInvite, str]:
    if role not in {"factory", "broker"} or max_uses < 1:
        raise ValueError("invalid invite")
    token = secrets.token_urlsafe(24)
    invite = AibotRoleInvite(
        invite_id=str(uuid.uuid4()), token_digest=token_digest(token), target_role=role,
        expires_at=expires_at, max_uses=max_uses, created_by=operator,
    )
    db.add(invite)
    db.flush()
    return invite, token


def apply_invite(db: Session, *, binding: AibotIdentityBinding, token: str, requested_role: str | None = None) -> AibotRegistration:
    if binding.binding_status != "active" or not binding.canonical_userid:
        raise ValueError("verified active binding required")
    invite = db.query(AibotRoleInvite).filter(AibotRoleInvite.token_digest == token_digest(token)).first()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if invite is None or invite.revoked_at is not None or invite.expires_at <= now or invite.used_count >= invite.max_uses:
        raise ValueError("invite is invalid or expired")
    if requested_role and requested_role != invite.target_role:
        raise ValueError("requested role does not match invite")
    registration = db.query(AibotRegistration).filter(AibotRegistration.identity_binding_id == binding.binding_id).first()
    if registration is None:
        registration = AibotRegistration(
            registration_id=str(uuid.uuid4()), canonical_userid=binding.canonical_userid,
            identity_binding_id=binding.binding_id, registration_status="pending_role",
            registration_source="invite", requested_role=invite.target_role,
        )
        db.add(registration)
    elif registration.registration_status == "active" and registration.granted_role == invite.target_role:
        return registration
    else:
        registration.registration_status = "pending_role"
        registration.registration_source = "invite"
        registration.requested_role = invite.target_role
    invite.used_count += 1
    db.flush()
    _audit(db, bot_id=binding.bot_id, digest=binding.opaque_actor_digest, action="invite_applied", result="pending_role", canonical=binding.canonical_userid, metadata={"invite_id": invite.invite_id, "role": invite.target_role})
    return registration


def approve_role(db: Session, *, registration_id: str, operator: str) -> AibotRegistration:
    registration = db.query(AibotRegistration).filter(AibotRegistration.registration_id == registration_id).first()
    if registration is None or registration.registration_status != "pending_role" or registration.requested_role not in {"factory", "broker"}:
        raise ValueError("registration is not pending approval")
    user = db.query(User).filter(User.external_userid == registration.canonical_userid).first()
    if user is None or user.status != "active":
        raise ValueError("active canonical user is required")
    user.role = registration.requested_role
    user.can_search_jobs = 0 if user.role == "factory" else 1
    user.can_search_workers = 1
    registration.granted_role = registration.requested_role
    registration.registration_status = "active"
    binding = db.query(AibotIdentityBinding).filter(AibotIdentityBinding.binding_id == registration.identity_binding_id).first()
    if binding:
        binding.approved_by = operator
        binding.approved_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.flush()
    _audit(db, bot_id=binding.bot_id if binding else "", digest=binding.opaque_actor_digest if binding else "", action="role_approved", result="active", canonical=registration.canonical_userid, actor=operator, metadata={"role": registration.granted_role})
    return registration


def revoke_binding(db: Session, *, binding_id: str, operator: str, reason: str = "admin_revoked") -> None:
    binding = db.query(AibotIdentityBinding).filter(AibotIdentityBinding.binding_id == binding_id).first()
    if binding is None:
        raise ValueError("binding not found")
    binding.binding_status = "revoked"
    binding.revoked_by = operator
    binding.revoked_at = datetime.now(timezone.utc).replace(tzinfo=None)
    registration = db.query(AibotRegistration).filter(AibotRegistration.identity_binding_id == binding_id).first()
    if registration:
        registration.registration_status = "revoked"
    _audit(db, bot_id=binding.bot_id, digest=binding.opaque_actor_digest, action="binding_revoked", result="revoked", canonical=binding.canonical_userid, actor=operator, reason=reason)
    db.flush()
