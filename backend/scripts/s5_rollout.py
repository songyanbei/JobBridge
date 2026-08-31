"""S5 direction-scoped rollout/rollback operator helper (no business writes)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domains.recruitment.matching import DIRECTIONS
from app.services.resume_rollout_service import direction_allowed


ROLLBACK_ORDER = ("contact_off", "resume_search_kill_switch", "resume_publish_kill_switch", "action_off", "legacy_fallback", "stop_phase15_consumer")


def plan(*, directions=None, percentage: int = 0, kill_switch: bool = True) -> dict:
    selected = list(directions or DIRECTIONS)
    return {
        "mode": "rollback" if kill_switch else "rollout",
        "percentage": max(0, min(100, int(percentage))),
        "directions": [item for item in selected if direction_allowed(item, selected)],
        "kill_switch": bool(kill_switch),
        "rollback_order": list(ROLLBACK_ORDER),
        "legacy_default": True,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--percentage", type=int, default=0)
    parser.add_argument("--direction", action="append", dest="directions")
    parser.add_argument("--kill-switch", action="store_true", default=False)
    args = parser.parse_args()
    print(json.dumps(plan(directions=args.directions, percentage=args.percentage, kill_switch=args.kill_switch), ensure_ascii=True, sort_keys=True))
