"""Control-plane contracts for the isolated development demo mode.

This module deliberately stops at identity/context resolution.  It does not
modify the real user's role and does not make routing, session, or business
data decisions.  Callers own the surrounding transaction and authorization
of the operator invoking administrative mutations.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import settings
from app.models import DemoPrincipal, DemoResource, DemoWorkspace, DemoWorkspaceMember, User
from app.services.registration_service import actor_digest

_ROLES = ("worker", "factory", "broker")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class DemoModeError(ValueError):
    """Base class for deterministic, fail-closed demo control-plane errors."""


class DemoModeDisabled(DemoModeError):
    pass


class DemoAuthorizationError(DemoModeError):
    pass


class DemoWorkspaceStateError(DemoModeError):
    pass


@dataclass(frozen=True)
class DemoActorContext:
    """Resolved demo identity consumed by later worker/router integration."""

    demo_mode: bool
    demo_id: str
    real_actor_userid: str
    effective_userid: str
    active_role: str
    bot_id: str
    workspace_status: str

    @property
    def reply_userid(self) -> str:
        """The real actor remains the only valid external reply target."""
        return self.real_actor_userid


def actor_digest_for_lookup(actor_id: str) -> str:
    """Return the configured keyed digest without retaining/logging actor text."""
    return actor_digest(actor_id).lower()


def _require_digest(value: str) -> str:
    digest = str(value or "").strip().lower()
    if not _DIGEST_RE.fullmatch(digest):
        raise DemoAuthorizationError("actor digest must be a 64-character hex value")
    return digest


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _configured_bot_allowed(bot_id: str) -> bool:
    return bool(bot_id and bot_id.strip() in settings.demo_allowed_bot_id_list)


def ensure_demo_enabled(*, bot_id: str) -> None:
    """Enforce the process-level fail-closed gate before any demo operation."""
    if not settings.demo_mode_enabled:
        raise DemoModeDisabled("demo mode is disabled")
    if settings.app_env.lower().strip() not in {"development", "test"}:
        raise DemoModeDisabled("demo mode is unavailable outside development/test")
    if not _configured_bot_allowed(bot_id):
        raise DemoAuthorizationError("bot is not allowlisted for demo mode")


def actor_is_allowlisted(actor_digest_value: str) -> bool:
    return _require_digest(actor_digest_value) in settings.demo_allowed_actor_digest_list


def _workspace_or_raise(db: Session, demo_id: str) -> DemoWorkspace:
    workspace = db.query(DemoWorkspace).filter(DemoWorkspace.demo_id == demo_id).first()
    if workspace is None:
        raise DemoAuthorizationError("demo workspace not found")
    return workspace


def _active_member(
    db: Session,
    *,
    demo_id: str,
    bot_id: str,
    digest: str,
) -> DemoWorkspaceMember | None:
    member = db.query(DemoWorkspaceMember).filter(
        DemoWorkspaceMember.demo_id == demo_id,
        DemoWorkspaceMember.bot_id == bot_id,
        DemoWorkspaceMember.opaque_actor_digest == digest,
        DemoWorkspaceMember.membership_status == "active",
    ).first()
    if member is not None and member.expires_at is not None and member.expires_at <= _now():
        member.membership_status = "expired"
        db.flush()
        return None
    return member


def create_workspace(
    db: Session,
    *,
    name: str,
    bot_id: str,
    actor_digest_value: str,
    created_by: str,
    canonical_actor_userid: str | None = None,
    demo_id: str | None = None,
) -> DemoWorkspace:
    """Create one complete workspace and its three synthetic principals.

    The initial actor must be statically allowlisted.  Subsequent actors can
    be granted quickly via :func:`authorize_member`, which only needs the bot
    allowlist and the target workspace membership record.
    """
    ensure_demo_enabled(bot_id=bot_id)
    digest = _require_digest(actor_digest_value)
    if not actor_is_allowlisted(digest):
        raise DemoAuthorizationError("initial actor is not allowlisted for demo mode")
    if not str(name or "").strip() or len(name.strip()) > 128:
        raise DemoModeError("workspace name is required")
    active_count = db.query(DemoWorkspace).filter(DemoWorkspace.status == "active").count()
    if active_count >= settings.demo_max_active_workspaces:
        raise DemoWorkspaceStateError("maximum active demo workspaces reached")

    workspace = DemoWorkspace(
        demo_id=demo_id or f"demo-{uuid.uuid4().hex[:20]}",
        name=name.strip(), status="active", bot_id=bot_id.strip(),
        opaque_actor_digest=digest, canonical_actor_userid=canonical_actor_userid,
        created_by=str(created_by or "system")[:64],
    )
    db.add(workspace)
    db.flush()

    synthetic_principals: list[tuple[str, str]] = []
    for role in _ROLES:
        synthetic_userid = f"demo_{role}_{uuid.uuid4().hex[:16]}"
        db.add(User(
            external_userid=synthetic_userid,
            role=role,
            display_name=f"演示-{role}",
            can_search_jobs=1 if role in {"worker", "broker"} else 0,
            can_search_workers=1 if role in {"factory", "broker"} else 0,
            status="active",
            extra={"demo_id": workspace.demo_id, "demo_synthetic": True, "demo_role": role},
        ))
        synthetic_principals.append((role, synthetic_userid))

    # DemoPrincipal.synthetic_userid has an explicit RESTRICT FK to User. Do
    # not rely on SQLAlchemy's table ordering here: the two ORM objects are
    # intentionally decoupled, so flush the synthetic users first.
    db.flush()
    for role, synthetic_userid in synthetic_principals:
        db.add(DemoPrincipal(
            principal_id=str(uuid.uuid4()), demo_id=workspace.demo_id,
            role=role, synthetic_userid=synthetic_userid,
            principal_status="active",
        ))
    db.add(DemoWorkspaceMember(
        member_id=str(uuid.uuid4()), demo_id=workspace.demo_id, bot_id=bot_id.strip(),
        opaque_actor_digest=digest, canonical_actor_userid=canonical_actor_userid,
        membership_status="active", granted_by=str(created_by or "system")[:64],
    ))
    db.flush()
    return workspace


def authorize_member(
    db: Session,
    *,
    demo_id: str,
    bot_id: str,
    actor_digest_value: str,
    granted_by: str,
    canonical_actor_userid: str | None = None,
    expires_at: datetime | None = None,
) -> DemoWorkspaceMember:
    """Idempotently authorize another actor for an active workspace.

    The target is identified by bot ID and keyed actor digest.  The caller is
    expected to be an already-authorized admin/control-plane operator; this
    service does not infer admin privileges from a business User row.
    """
    ensure_demo_enabled(bot_id=bot_id)
    digest = _require_digest(actor_digest_value)
    workspace = _workspace_or_raise(db, demo_id)
    if workspace.bot_id != bot_id.strip() or workspace.status != "active":
        raise DemoWorkspaceStateError("workspace is not active for this bot")
    member = db.query(DemoWorkspaceMember).filter(
        DemoWorkspaceMember.demo_id == demo_id,
        DemoWorkspaceMember.bot_id == bot_id.strip(),
        DemoWorkspaceMember.opaque_actor_digest == digest,
    ).first()
    if member is None:
        member = DemoWorkspaceMember(
            member_id=str(uuid.uuid4()), demo_id=demo_id, bot_id=bot_id.strip(),
            opaque_actor_digest=digest, canonical_actor_userid=canonical_actor_userid,
            membership_status="active", granted_by=str(granted_by or "system")[:64],
            expires_at=expires_at,
        )
        db.add(member)
    else:
        member.membership_status = "active"
        member.granted_by = str(granted_by or "system")[:64]
        member.canonical_actor_userid = canonical_actor_userid or member.canonical_actor_userid
        member.expires_at = expires_at
        member.revoked_at = None
    db.flush()
    return member


def revoke_member(db: Session, *, demo_id: str, bot_id: str, actor_digest_value: str) -> None:
    ensure_demo_enabled(bot_id=bot_id)
    digest = _require_digest(actor_digest_value)
    member = db.query(DemoWorkspaceMember).filter(
        DemoWorkspaceMember.demo_id == demo_id,
        DemoWorkspaceMember.bot_id == bot_id.strip(),
        DemoWorkspaceMember.opaque_actor_digest == digest,
    ).first()
    if member is None:
        return
    member.membership_status = "revoked"
    member.revoked_at = _now()
    db.flush()


def get_active_workspace_for_actor(
    db: Session, *, bot_id: str, actor_digest_value: str,
) -> DemoWorkspace | None:
    ensure_demo_enabled(bot_id=bot_id)
    digest = _require_digest(actor_digest_value)
    # Static actor allowlisting is only a provisioning gate.  Runtime ingress
    # still requires an active workspace membership, so an allowlisted actor
    # cannot enter an unprovisioned workspace by itself.
    rows = db.query(DemoWorkspace).join(
        DemoWorkspaceMember, DemoWorkspaceMember.demo_id == DemoWorkspace.demo_id,
    ).filter(
        DemoWorkspace.status == "active",
        DemoWorkspaceMember.bot_id == bot_id.strip(),
        DemoWorkspaceMember.opaque_actor_digest == digest,
        DemoWorkspaceMember.membership_status == "active",
    ).order_by(DemoWorkspace.created_at.desc()).all()
    for workspace in rows:
        if _active_member(db, demo_id=workspace.demo_id, bot_id=bot_id, digest=digest):
            return workspace
    return None


def switch_role(
    db: Session,
    *,
    demo_id: str,
    bot_id: str,
    actor_digest_value: str,
    active_role: str,
) -> DemoActorContext:
    """Resolve a role-specific synthetic principal without changing User.role."""
    ensure_demo_enabled(bot_id=bot_id)
    digest = _require_digest(actor_digest_value)
    if active_role not in _ROLES:
        raise DemoModeError("active role must be worker, factory, or broker")
    workspace = _workspace_or_raise(db, demo_id)
    if workspace.bot_id != bot_id.strip() or workspace.status != "active":
        raise DemoWorkspaceStateError("workspace is not active for this bot")
    member = _active_member(db, demo_id=demo_id, bot_id=bot_id, digest=digest)
    if member is None:
        raise DemoAuthorizationError("actor is not an active workspace member")
    principal = db.query(DemoPrincipal).filter(
        DemoPrincipal.demo_id == demo_id,
        DemoPrincipal.role == active_role,
        DemoPrincipal.principal_status == "active",
    ).first()
    if principal is None:
        raise DemoWorkspaceStateError("workspace principal is unavailable")
    return DemoActorContext(
        demo_mode=True,
        demo_id=demo_id,
        real_actor_userid=member.canonical_actor_userid or workspace.canonical_actor_userid or "",
        effective_userid=principal.synthetic_userid,
        active_role=active_role,
        bot_id=bot_id.strip(),
        workspace_status=workspace.status,
    )


def register_resource(
    db: Session,
    *,
    demo_id: str,
    resource_type: str,
    target_id: str,
    metadata: dict | None = None,
) -> DemoResource:
    """Idempotently register a created demo resource for future cleanup."""
    workspace = _workspace_or_raise(db, demo_id)
    if workspace.status != "active":
        raise DemoWorkspaceStateError("workspace is not active")
    if not resource_type or not target_id:
        raise DemoModeError("resource type and target id are required")
    resource = db.query(DemoResource).filter(
        DemoResource.demo_id == demo_id,
        DemoResource.resource_type == resource_type,
        DemoResource.target_id == str(target_id),
    ).first()
    if resource is None:
        resource = DemoResource(
            resource_id=str(uuid.uuid4()), demo_id=demo_id,
            resource_type=resource_type, target_id=str(target_id), metadata_json=metadata,
        )
        db.add(resource)
    elif metadata is not None:
        resource.metadata_json = metadata
    db.flush()
    return resource


def demo_request_allowed(db: Session, *, bot_id: str, actor_digest_value: str) -> bool:
    """Non-throwing gate for ingress callers that need a simple boolean."""
    try:
        ensure_demo_enabled(bot_id=bot_id)
        digest = _require_digest(actor_digest_value)
        if actor_is_allowlisted(digest):
            return True
        return get_active_workspace_for_actor(db, bot_id=bot_id, actor_digest_value=digest) is not None
    except DemoModeError:
        return False
