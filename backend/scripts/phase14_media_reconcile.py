"""Read-only/apply reconciliation for S4 job media version bindings.

The script is intentionally conservative: it never deletes objects.  It
reports references that are missing from the lifecycle table and can backfill
the owning Job.version for unversioned attached rows.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import Job, MediaAssetLifecycle
from app.services.storage_reference_service import normalize_storage_reference


def reconcile(db: Session, *, apply: bool = False) -> dict[str, int]:
    stats = Counter()
    jobs = db.query(Job).order_by(Job.id).all()
    for job in jobs:
        images = job.images or []
        if not isinstance(images, list):
            stats["invalid_images"] += 1
            continue
        for value in images:
            try:
                key = normalize_storage_reference(value)
            except (TypeError, ValueError):
                stats["invalid_keys"] += 1
                continue
            row = db.query(MediaAssetLifecycle).filter_by(object_key=key).one_or_none()
            if row is None:
                stats["missing_lifecycle"] += 1
                continue
            if row.owner_userid != job.owner_userid:
                stats["owner_conflict"] += 1
                continue
            if row.entity_type == "job" and row.entity_id == job.id and row.state == "attached":
                if row.entity_version is None:
                    stats["unversioned_attached"] += 1
                    if apply:
                        row.entity_version = int(job.version or 1)
                        stats["backfilled"] += 1
                elif int(row.entity_version) != int(job.version or 1):
                    stats["version_mismatch"] += 1
            elif row.state == "attached":
                stats["wrong_entity"] += 1
    if apply:
        db.commit()
    return dict(stats)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    with SessionLocal() as db:
        report = reconcile(db, apply=args.apply)
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return 0 if not any(report.get(k, 0) for k in ("missing_lifecycle", "owner_conflict", "wrong_entity")) else 2


if __name__ == "__main__":
    raise SystemExit(main())
