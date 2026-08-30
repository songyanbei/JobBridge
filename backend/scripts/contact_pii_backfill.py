"""Resumable, bounded Contact PII ciphertext backfill (B3).

Run with ``--apply`` only after B1 migration and key-ring readiness checks. A
failure pauses the entity and leaves legacy values untouched for inspection.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db import SessionLocal
from app.models import Job, User
from app.services.pii_crypto_service import PiiCryptoError, PiiCryptoService

ENTITIES = {"user": (User, "external_userid"), "job": (Job, "id")}
FIELDS = ("phone", "contact_person")


def _state(db, entity: str):
    return db.execute(text("SELECT entity,last_pk,success_count,error_count,status FROM contact_pii_migration_state WHERE entity=:entity FOR UPDATE"), {"entity": entity}).mappings().first()


def run(*, apply: bool, entity: str = "all", batch_size: int = 100, crypto: PiiCryptoService | None = None, db_factory=SessionLocal) -> dict:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    entities = list(ENTITIES) if entity == "all" else [entity]
    if any(item not in ENTITIES for item in entities):
        raise ValueError("entity must be user, job or all")
    crypto = crypto or PiiCryptoService.from_settings()
    result = {item: {"scanned": 0, "encrypted": 0, "errors": 0, "status": "pending"} for item in entities}
    with db_factory() as db:
        for item in entities:
            model, pk_name = ENTITIES[item]
            state = _state(db, item)
            if state is None:
                raise RuntimeError("contact_pii_migration_state_missing")
            if state["status"] == "completed":
                result[item]["status"] = "completed"
                continue
            last_pk = int(state["last_pk"] or 0) if item == "job" else str(state["last_pk"] or "")
            if apply:
                db.execute(text("UPDATE contact_pii_migration_state SET status='running', last_error_code=NULL WHERE entity=:entity"), {"entity": item})
                db.commit()
            try:
                query = db.query(model).filter(getattr(model, pk_name) > last_pk).order_by(getattr(model, pk_name)).limit(batch_size).all()
                for row in query:
                    result[item]["scanned"] += 1
                    row_pk = str(getattr(row, pk_name))
                    changed = False
                    for field in FIELDS:
                        plaintext = getattr(row, field, None)
                        ciphertext = getattr(row, f"{field}_ciphertext", None)
                        if plaintext and not ciphertext:
                            sealed = crypto.encrypt(plaintext, field=field, entity_type=item, entity_id=row_pk)
                            if apply:
                                setattr(row, f"{field}_ciphertext", sealed.value)
                                setattr(row, f"{field}_key_version", sealed.key_version)
                                setattr(row, f"{field}_digest", sealed.digest)
                            result[item]["encrypted"] += 1
                            changed = True
                    if apply:
                        db.execute(text("UPDATE contact_pii_migration_state SET last_pk=:pk, success_count=success_count+:count WHERE entity=:entity"), {"pk": row_pk, "count": int(changed), "entity": item})
                        db.commit()
                if len(query) < batch_size and apply:
                    db.execute(text("UPDATE contact_pii_migration_state SET status='completed' WHERE entity=:entity"), {"entity": item})
                    db.commit()
                    result[item]["status"] = "completed"
                else:
                    result[item]["status"] = "running" if apply else "pending"
            except Exception as exc:
                if apply:
                    db.rollback()
                    db.execute(text("UPDATE contact_pii_migration_state SET status='paused', error_count=error_count+1, last_error_code=:code WHERE entity=:entity"), {"code": "key_unavailable" if isinstance(exc, PiiCryptoError) else "backfill_error", "entity": item})
                    db.commit()
                result[item]["errors"] += 1
                result[item]["status"] = "paused"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--entity", choices=["all", "user", "job"], default="all")
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()
    print(json.dumps(run(apply=args.apply, entity=args.entity, batch_size=args.batch_size), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
