"""Turn-scoped demo isolation primitives.

The message worker binds this context after the real AIBot actor has been
verified.  Lower-level business services can then stamp newly-created rows
and register them for cleanup without threading a demo argument through every
legacy function signature.
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from sqlalchemy import false
from sqlalchemy.orm import Session


class DemoScopeError(ValueError):
    """Raised when a demo turn cannot be safely scoped."""


@dataclass(frozen=True)
class DemoScope:
    demo_id: str
    real_actor_userid: str
    effective_userid: str
    active_role: str
    bot_id: str = ""
    actor_digest: str | None = None


_current_scope: ContextVar[DemoScope | None] = ContextVar(
    "jobbridge_demo_scope",
    default=None,
)


def bind(scope: DemoScope | None) -> Token:
    """Bind one scope for the current worker turn."""
    return _current_scope.set(scope)


def reset(token: Token) -> None:
    _current_scope.reset(token)


def current() -> DemoScope | None:
    return _current_scope.get()


def from_context(context: Any | None) -> DemoScope | None:
    """Convert a message-side DemoActorContext to the lower-level scope."""
    if context is None:
        return None
    try:
        if not context.is_usable():
            raise DemoScopeError("demo context is not usable")
        values = {
            "demo_id": str(context.demo_id or "").strip(),
            "real_actor_userid": str(context.real_actor_userid or "").strip(),
            "effective_userid": str(context.effective_userid or "").strip(),
            "active_role": str(context.active_role or "").strip(),
            "bot_id": str(getattr(context, "bot_id", "") or "").strip(),
            "actor_digest": getattr(context, "actor_digest", None),
        }
    except AttributeError as exc:
        raise DemoScopeError("invalid demo context") from exc
    if not values["demo_id"] or not values["real_actor_userid"] or not values["effective_userid"]:
        raise DemoScopeError("demo context is incomplete")
    return DemoScope(**values)


def demo_id_or_none() -> str | None:
    scope = current()
    return scope.demo_id if scope is not None else None


def stamp(row: Any, *, demo_id: str | None = None) -> Any:
    """Stamp a mapped row when its model has a demo_id column."""
    value = demo_id if demo_id is not None else demo_id_or_none()
    if value is not None and hasattr(row, "demo_id"):
        row.demo_id = value
    return row


def register(
    db: Session,
    resource_type: str,
    target_id: Any,
    *,
    metadata: dict | None = None,
    demo_id: str | None = None,
) -> Any | None:
    """Register a newly-created resource only for the active demo turn.

    Registration is intentionally best-effort for normal turns and strict for
    demo turns.  A missing/invalid registry write must abort a demo transaction
    rather than leave an uncleanable row behind.
    """
    value = demo_id if demo_id is not None else demo_id_or_none()
    if value is None:
        return None
    if not resource_type or target_id is None:
        raise DemoScopeError("demo resource type and target id are required")
    from app.services.demo_mode_service import register_resource

    return register_resource(
        db,
        demo_id=value,
        resource_type=resource_type,
        target_id=str(target_id),
        metadata=metadata,
    )


def apply(query: Any, model: Any, *, demo_id: str | None = None, required: bool = False) -> Any:
    """Apply exact demo filtering; invalid demo scope fails closed."""
    value = demo_id if demo_id is not None else demo_id_or_none()
    if value is None and not required:
        return query
    column = getattr(model, "demo_id", None)
    if not value or column is None:
        return query.filter(false())
    return query.filter(column == value)


def require(model: Any, *, demo_id: str | None = None) -> str:
    """Validate that a model query can be scoped to an exact workspace."""
    value = demo_id if demo_id is not None else demo_id_or_none()
    if not value or not hasattr(model, "demo_id"):
        raise DemoScopeError(f"model {getattr(model, '__tablename__', model)!s} lacks a demo scope")
    return value
