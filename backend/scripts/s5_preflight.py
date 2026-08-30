"""Read-only S5 contract preflight. Exits non-zero on unsafe configuration."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.domains.recruitment.matching import DIRECTIONS, POLICY_VERSION
from app.listing.resume_profile import RESUME_PROFILE


def run() -> dict:
    checks = {
        "profile": RESUME_PROFILE.name == "recruitment.resume",
        "profile_version": bool(RESUME_PROFILE.version),
        "directions": len(DIRECTIONS) == 4,
        "matching_policy": POLICY_VERSION == "matching-policy-v1",
        "publish_default_off": not bool(getattr(settings, "resume_publish_flow_enabled", False)),
        "search_default_off": not bool(getattr(settings, "resume_search_facade_enabled", False)),
        "kill_switch_default": bool(getattr(settings, "resume_publish_kill_switch", True)),
    }
    return {"ready": all(checks.values()), "checks": checks}


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    sys.exit(0 if result["ready"] else 1)
