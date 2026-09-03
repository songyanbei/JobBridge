"""Compatibility provider for the message-side demo context adapter.

The message adapter intentionally imports this module by contract.  Keep all
database authority in :mod:`demo_mode_service`; this module only converts the
verified real actor into the adapter's turn-scoped context and never changes a
real user's role.
"""
from __future__ import annotations

from dataclasses import replace

from app.models import DemoPrincipal, DemoWorkspace, DemoWorkspaceMember
from app.services.demo_message_context import DemoActorContext, load_active_context
from app.services.demo_mode_service import (
    DemoModeError,
    _now,
    actor_digest_for_lookup,
    get_active_workspace_for_actor,
    switch_role,
)


def activate_for_actor(db, actor_userid: str, bot_id: str, role: str) -> DemoActorContext | None:
    """Activate ``role`` for a verified actor using workspace membership."""
    if not actor_userid or not bot_id:
        return None
    try:
        digest = actor_digest_for_lookup(actor_userid)
        workspace = get_active_workspace_for_actor(db, bot_id=bot_id, actor_digest_value=digest)
        if workspace is None:
            return None
        resolved = switch_role(
            db,
            demo_id=workspace.demo_id,
            bot_id=bot_id,
            actor_digest_value=digest,
            active_role=role,
        )
        # The caller supplied the already verified actor identity.  Keep it as
        # the reply target even when the membership was provisioned before the
        # canonical userid was available.
        return DemoActorContext(
            demo_mode=resolved.demo_mode,
            demo_id=resolved.demo_id,
            real_actor_userid=actor_userid,
            effective_userid=resolved.effective_userid,
            active_role=resolved.active_role,
            bot_id=resolved.bot_id,
            workspace_status=resolved.workspace_status,
            actor_digest=digest,
        )
    except DemoModeError:
        return None


def resolve_for_actor(
    db,
    actor_userid: str,
    bot_id: str,
    conversation_type: str = "single",
    conversation_id: str = "",
) -> DemoActorContext | None:
    """Refresh the pointer's role against the authoritative workspace state."""
    if not actor_userid or not bot_id:
        return None
    pointer = load_active_context(
        actor_userid,
        conversation_type=conversation_type,
        conversation_id=conversation_id,
    )
    if pointer is None or not pointer.active_role:
        return None
    # The Redis pointer names the exact isolated workspace.  Do not resolve
    # through get_active_workspace_for_actor(), which intentionally selects the
    # newest workspace and can make a long-lived conversation drift between
    # demo datasets when the actor has multiple workspaces.
    try:
        digest = actor_digest_for_lookup(actor_userid)
        workspace = db.query(DemoWorkspace).filter(
            DemoWorkspace.demo_id == pointer.demo_id,
        ).first()
        if workspace is None or workspace.bot_id != bot_id.strip():
            return None
        principal = db.query(DemoPrincipal).filter(
            DemoPrincipal.demo_id == workspace.demo_id,
            DemoPrincipal.role == pointer.active_role,
        ).first()
        if principal is None:
            return None
        member = db.query(DemoWorkspaceMember).filter(
            DemoWorkspaceMember.demo_id == workspace.demo_id,
            DemoWorkspaceMember.bot_id == bot_id.strip(),
            DemoWorkspaceMember.opaque_actor_digest == digest,
        ).first()
        if workspace.status == "active":
            if member is None or member.membership_status != "active":
                return None
            if member.expires_at is not None and member.expires_at <= _now():
                return None
        refreshed = DemoActorContext(
            demo_mode=True,
            demo_id=workspace.demo_id,
            real_actor_userid=actor_userid,
            effective_userid=principal.synthetic_userid,
            active_role=pointer.active_role,
            bot_id=bot_id.strip(),
            workspace_status=workspace.status,
        )
    except DemoModeError:
        return None
    return replace(
        refreshed,
        conversation_type=conversation_type or "single",
        conversation_id=conversation_id or actor_userid,
    )


def deactivate_for_actor(db, actor_userid: str, bot_id: str) -> bool:
    """Validate the actor/workspace pair; pointer removal is adapter-owned."""
    if not actor_userid or not bot_id:
        return False
    try:
        digest = actor_digest_for_lookup(actor_userid)
        return get_active_workspace_for_actor(db, bot_id=bot_id, actor_digest_value=digest) is not None
    except DemoModeError:
        return False
