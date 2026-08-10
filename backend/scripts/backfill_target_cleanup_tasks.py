"""Backfill one durable target-cleanup task for every soft-deleted job."""
from __future__ import annotations

import argparse

from app.db import SessionLocal
from app.models import Job, TargetCleanupTask
from app.services.target_cleanup_service import upsert_job_cleanup_task


def run(*, apply: bool, batch_size: int = 500) -> dict[str, int]:
    scanned = created = missing_count = 0
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
                covered = {
                    row[0] for row in db.query(TargetCleanupTask.target_id).filter(
                        TargetCleanupTask.target_type == "job",
                        TargetCleanupTask.target_id.in_(ids),
                    ).all()
                }
                missing_count += len(set(ids) - covered)
            else:
                missing_count += len(missing)
            last_id = ids[-1]
    return {"scanned": scanned, "created": created, "missing": missing_count}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()
    print(run(apply=args.apply, batch_size=args.batch_size))
