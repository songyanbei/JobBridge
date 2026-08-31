"""Scheduled Domain Outbox consumer entry point (Phase 14)."""
from __future__ import annotations

import os
from typing import Callable

from app.config import settings
from app.db import SessionLocal
from app.services.domain_outbox_service import consume_pending_events


def run_once(handler: Callable, *, owner: str | None = None) -> dict[str, int]:
    """Consume one bounded batch; caller owns any external scheduler lock."""
    if not settings.domain_outbox_consumer_enabled:
        return {"claimed": 0, "published": 0, "retryable": 0, "dead_letter": 0, "stale": 0}
    consumer_owner = owner or os.environ.get("HOSTNAME") or "domain-outbox-consumer"
    with SessionLocal() as db:
        return consume_pending_events(
            db, handler, owner=consumer_owner,
            lease_seconds=settings.domain_outbox_consumer_lease_seconds,
            max_attempts=settings.domain_outbox_consumer_max_attempts,
        )


def run(handler: Callable) -> dict[str, int]:
    return run_once(handler)


__all__ = ["run", "run_once"]
