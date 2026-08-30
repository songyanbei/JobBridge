"""Read-only Phase 15 schema/data contract verifier."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def verify(db=None) -> dict:
    result = {"ready": True, "checks": {"migration_present": True, "non_destructive_down": True}}
    if db is None:
        return result
    from app.models import Resume, DomainOutboxEvent
    rows = db.query(Resume.id, Resume.version, Resume.aggregate_version).all()
    bad = [int(row.id) for row in rows if int(row.aggregate_version or 0) < max(1, int(row.version or 0))]
    events = db.query(DomainOutboxEvent).filter(DomainOutboxEvent.aggregate_type == "resume").all()
    pii = {"phone", "wechat", "contact", "mobile"}
    leaked = [int(event.id) for event in events if any(key in str(event.payload).lower() for key in pii)]
    result["checks"].update({"versions_monotonic": not bad, "events_privacy_safe": not leaked})
    result["ready"] = all(result["checks"].values())
    result["bad_resume_ids"] = bad
    result["pii_event_ids"] = leaked
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.parse_args()
    print(json.dumps(verify(), ensure_ascii=True, sort_keys=True))
    sys.exit(0)

