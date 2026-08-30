"""Install the B3 legacy-column write freeze after migration verification."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db import SessionLocal


def _ready(db) -> bool:
    rows = db.execute(text("SELECT entity,status FROM contact_pii_migration_state WHERE entity IN ('user','job')")).mappings().all()
    return {row["entity"]: row["status"] for row in rows} == {"user": "completed", "job": "completed"}


def install(*, apply: bool, db_factory=SessionLocal) -> dict:
    with db_factory() as db:
        if not _ready(db):
            return {"status": "blocked", "reason": "migration_incomplete"}
        triggers = {"trg_user_contact_legacy_freeze": "user", "trg_job_contact_legacy_freeze": "job"}
        installed = []
        for trigger, table in triggers.items():
            exists = db.execute(text("SELECT COUNT(*) FROM information_schema.triggers WHERE trigger_schema=DATABASE() AND trigger_name=:name"), {"name": trigger}).scalar()
            if exists:
                installed.append(trigger)
                continue
            if not apply:
                continue
            body = (
                f"CREATE TRIGGER `{trigger}` BEFORE UPDATE ON `{table}` FOR EACH ROW "
                "BEGIN IF NOT (NEW.phone <=> OLD.phone) OR NOT (NEW.contact_person <=> OLD.contact_person) "
                "THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='contact legacy PII columns are read-only'; END IF; END"
            )
            db.execute(text(body))
            installed.append(trigger)
        if apply:
            db.commit()
        return {"status": "frozen" if apply else "ready", "triggers": installed}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = install(apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"ready", "frozen"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
