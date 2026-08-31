"""Scheduled Domain Outbox consumer entry point (Phase 14)."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Callable

from app.config import settings
from app.db import SessionLocal
from app.services.domain_outbox_service import consume_pending_events


_last_run_at: datetime | None = None
_last_result: dict[str, int] | None = None


def default_handler(event) -> None:
    """Default projection hook: event validation is complete before acking.

    Deployments may replace this with an index/recommendation projector. The
    default intentionally has no external side effect, so facts remain safe if
    optional projections are unavailable.
    """
    del event


def health_snapshot() -> dict[str, object]:
    return {
        "healthy": bool(_last_run_at is not None),
        "last_run_at": _last_run_at.isoformat() if _last_run_at else None,
        "last_result": dict(_last_result or {}),
    }


def run_once(handler: Callable | None = None, *, owner: str | None = None) -> dict[str, int]:
    """Consume one bounded batch; caller owns any external scheduler lock."""
    global _last_run_at, _last_result
    if not settings.domain_outbox_consumer_enabled:
        result = {"claimed": 0, "published": 0, "retryable": 0, "dead_letter": 0, "stale": 0}
        _last_run_at = datetime.now(timezone.utc)
        _last_result = result
        return result
    consumer_owner = owner or os.environ.get("HOSTNAME") or "domain-outbox-consumer"
    with SessionLocal() as db:
        result = consume_pending_events(
            db, handler or default_handler, owner=consumer_owner,
            lease_seconds=settings.domain_outbox_consumer_lease_seconds,
            max_attempts=settings.domain_outbox_consumer_max_attempts,
        )
    _last_run_at = datetime.now(timezone.utc)
    _last_result = result
    return result


def run(handler: Callable | None = None) -> dict[str, int]:
    return run_once(handler)


__all__ = ["default_handler", "health_snapshot", "run", "run_once"]
