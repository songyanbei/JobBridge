"""S4 job publish rollout and emergency rollback helpers.

This command is deliberately side-effect free by default.  Operators can use
the JSON output to apply the corresponding config revision through the normal
admin control plane; ``--rollback`` prints the ordered fail-closed actions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from typing import Any


def actor_bucket(actor_id: str, *, scope: str = "job_publish") -> int:
    digest = hashlib.sha256(f"{scope}:{actor_id}".encode()).hexdigest()
    return int(digest[:8], 16) % 100


def rollout_assignment(actor_id: str, percentage: int, *, scope: str = "job_publish") -> bool:
    pct = max(0, min(100, int(percentage)))
    return bool(actor_id) and actor_bucket(actor_id, scope=scope) < pct


def rollback_plan() -> list[dict[str, str]]:
    return [
        {"action": "set", "key": "job_publish_kill_switch", "value": "true"},
        {"action": "set", "key": "action_execution_mode", "value": "off"},
        {"action": "route", "key": "job_publish_flow", "value": "legacy"},
        {"action": "stop", "key": "domain_outbox_consumer", "value": "phase14"},
    ]


def evaluate_gate(config: Any) -> dict[str, Any]:
    """Return a fail-closed gate report without touching database state."""
    enabled = bool(getattr(config, "job_publish_flow_enabled", False))
    kill = bool(getattr(config, "job_publish_kill_switch", False))
    percentage = max(0, min(100, int(getattr(config, "job_publish_rollout_percentage", 0))))
    return {
        "enabled": enabled,
        "kill_switch": kill,
        "rollout_percentage": percentage,
        "ready": enabled and not kill and percentage > 0,
        "rollback": rollback_plan(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    if args.rollback:
        print(json.dumps({"mode": "emergency_rollback", "steps": rollback_plan()}, ensure_ascii=True))
        return 0
    from app.config import settings
    print(json.dumps(evaluate_gate(settings), ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
