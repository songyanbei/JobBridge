"""Backfill one durable target-cleanup task for every soft-deleted job."""
from __future__ import annotations

import argparse
import json

from sqlalchemy import and_, func

from app.db import SessionLocal
from app.models import Job, TargetCleanupTask
from app.services.target_cleanup_service import upsert_job_cleanup_task


def count_missing_cleanup_tasks(db) -> int:
    return int(
        db.query(func.count(Job.id))
        .outerjoin(
            TargetCleanupTask,
            and_(
                TargetCleanupTask.target_type == "job",
                TargetCleanupTask.target_id == Job.id,
            ),
        )
        .filter(
            Job.deleted_at.isnot(None),
            TargetCleanupTask.id.is_(None),
        )
        .scalar()
        or 0
    )


def run(*, apply: bool, batch_size: int = 500) -> dict[str, int]:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    scanned = created = 0
    last_id = 0
    with SessionLocal() as db:
        while True:
            ids = [row[0] for row in db.query(Job.id).filter(
                Job.id > last_id,
                Job.deleted_at.isnot(None),
            ).order_by(Job.id).limit(batch_size).all()]
            if not ids:
                break
            existing = {
                row[0] for row in db.query(TargetCleanupTask.target_id).filter(
                    TargetCleanupTask.target_type == "job",
                    TargetCleanupTask.target_id.in_(ids),
                ).all()
            }
            scanned += len(ids)
            missing = [job_id for job_id in ids if job_id not in existing]
            if apply:
                for job_id in missing:
                    _, was_created = upsert_job_cleanup_task(
                        db, job_id, reason="historical_soft_delete",
                    )
                    created += int(was_created)
                db.commit()
            last_id = ids[-1]

        # Start a fresh RR snapshot so jobs soft-deleted behind the keyset cursor
        # and tasks created by racing writers are reflected in final coverage.
        db.commit()
        missing_count = count_missing_cleanup_tasks(db)
    return {"scanned": scanned, "created": created, "missing": missing_count}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()
    result = run(apply=args.apply, batch_size=args.batch_size)
    print(json.dumps({
        "mode": "apply" if args.apply else "dry-run",
        **result,
    }, ensure_ascii=False, indent=2))
    return 0 if result["missing"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
