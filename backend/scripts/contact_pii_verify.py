"""Read-only verification and freeze gate for Contact PII migration (B3)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from sqlalchemy import func

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db import SessionLocal
from app.models import Job, User


def _counts(db, model) -> dict[str, int]:
    total = int(db.query(func.count()).select_from(model).scalar() or 0)
    missing_phone = int(db.query(func.count()).select_from(model).filter(model.phone.isnot(None), model.phone_ciphertext.is_(None)).scalar() or 0)
    missing_person = int(db.query(func.count()).select_from(model).filter(model.contact_person.isnot(None), model.contact_person_ciphertext.is_(None)).scalar() or 0)
    return {"total": total, "missing_phone_ciphertext": missing_phone, "missing_contact_person_ciphertext": missing_person}


def verify(*, db_factory=SessionLocal) -> dict:
    with db_factory() as db:
        result = {"user": _counts(db, User), "job": _counts(db, Job)}
        result["ready_for_freeze"] = all(
            values["missing_phone_ciphertext"] == 0 and values["missing_contact_person_ciphertext"] == 0
            for values in result.values() if isinstance(values, dict)
        )
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    result = verify()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ready_for_freeze"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
