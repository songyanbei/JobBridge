"""Deterministic policy gates for the S4 job publishing flow."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.listing.job_profile import JOB_ACTIONS, PROFILE_VERSION


@dataclass(frozen=True)
class PublishDecision:
    allowed: bool
    reason: str
    action_name: str
    profile_version: str = PROFILE_VERSION
    rollout_percentage: int = 0


def bucket_for(*parts: Any) -> int:
    digest = hashlib.sha256(":".join(str(part) for part in parts).encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100


def is_rollout_target(actor_id: str, *, percentage: int | None = None, scope: str = "job_publish") -> bool:
    pct = int(getattr(settings, "job_publish_rollout_percentage", 0) if percentage is None else percentage)
    pct = max(0, min(100, pct))
    return pct > 0 and bucket_for(scope, actor_id) < pct


def evaluate_publish_policy(
    *, actor_id: str | None, role: str | None, action_name: str,
    enabled: bool | None = None, kill_switch: bool | None = None,
    rollout_percentage: int | None = None,
) -> PublishDecision:
    pct = int(getattr(settings, "job_publish_rollout_percentage", 0) if rollout_percentage is None else rollout_percentage)
    pct = max(0, min(100, pct))
    enabled = bool(getattr(settings, "job_publish_flow_enabled", False) if enabled is None else enabled)
    kill_switch = bool(getattr(settings, "job_publish_kill_switch", True) if kill_switch is None else kill_switch)
    if action_name not in JOB_ACTIONS:
        return PublishDecision(False, "unsupported_action", action_name, rollout_percentage=pct)
    if kill_switch:
        return PublishDecision(False, "kill_switch", action_name, rollout_percentage=pct)
    if not enabled:
        return PublishDecision(False, "feature_disabled", action_name, rollout_percentage=pct)
    if role not in {"factory", "broker"}:
        return PublishDecision(False, "role_not_allowed", action_name, rollout_percentage=pct)
    if not actor_id:
        return PublishDecision(False, "actor_required", action_name, rollout_percentage=pct)
    if not is_rollout_target(actor_id, percentage=pct):
        return PublishDecision(False, "rollout_control", action_name, rollout_percentage=pct)
    return PublishDecision(True, "enabled", action_name, rollout_percentage=pct)


__all__ = ["PublishDecision", "bucket_for", "evaluate_publish_policy", "is_rollout_target"]
