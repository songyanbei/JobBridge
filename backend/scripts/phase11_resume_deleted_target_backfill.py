"""Backfill resumable target-cleanup tasks for legacy soft-deleted resumes."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.phase11_cli_safety import run_safely  # noqa: E402

MIGRATION_KEY = "phase11_resume_deleted_target_backfill"


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")).hexdigest()


def _cursor(raw: str) -> dict[str, Any]:
    value = json.loads(raw or "{}")
    if not isinstance(value, dict):
        raise ValueError("resume cursor must be an object")
    summary = value.get("audit_summary")
    if summary is None:
        summary = {
            "last_resume_id": max(0, int(value.get("last_resume_id", 0) or 0)),
            "found": 0,
            "created": 0,
        }
    if not isinstance(summary, dict):
        raise ValueError("invalid audit summary")
    normalized = {
        "last_resume_id": max(0, int(summary.get("last_resume_id", 0) or 0)),
        "found": max(0, int(summary.get("found", 0) or 0)),
        "created": max(0, int(summary.get("created", 0) or 0)),
    }
    if normalized["last_resume_id"] != max(
        0, int(value.get("last_resume_id", normalized["last_resume_id"]) or 0)
    ):
        raise ValueError("cursor audit state mismatch")
    return {"last_resume_id": normalized["last_resume_id"], "audit_summary": normalized}


def _locked_checkpoint(conn, expected: dict[str, Any], updated: dict[str, Any]) -> None:
    row = conn.execute(text("""SELECT resume_cursor_json,verification_digest
      FROM phase11_migration_ledger WHERE migration_key=:key FOR UPDATE"""),
      {"key": MIGRATION_KEY}).mappings().one()
    raw_cursor = row["resume_cursor_json"]
    stored = _cursor(raw_cursor if isinstance(raw_cursor, str) else json.dumps(raw_cursor or {}))
    if stored != expected:
        raise RuntimeError("python_checkpoint_cursor_drift")
    stored_summary = stored["audit_summary"]
    stored_digest = row["verification_digest"]
    pristine = stored_summary == {"last_resume_id": 0, "found": 0, "created": 0}
    if (stored_digest is None and not pristine) or (
        stored_digest is not None and stored_digest != _digest(stored_summary)
    ):
        raise RuntimeError("python_checkpoint_digest_drift")
    summary = updated["audit_summary"]
    conn.execute(text("""UPDATE phase11_migration_ledger
      SET resume_cursor_json=:cursor,verification_digest=:digest
      WHERE migration_key=:key"""), {
        "cursor": json.dumps(updated, separators=(",", ":"), sort_keys=True),
        "digest": _digest(summary), "key": MIGRATION_KEY,
    })


def _after_checkpoint() -> None:
    if os.getenv("PHASE11_TEST_CRASH_AFTER_CHECKPOINT") == "1":
        os._exit(86)


def reconcile(dsn: str, *, apply: bool, batch_size: int,
              cursor: int | dict[str, Any]) -> dict:
    if not 1 <= batch_size <= 5000: raise ValueError("batch_size must be 1..5000")
    state = _cursor(json.dumps({"last_resume_id": cursor} if isinstance(cursor, int) else cursor))
    audit = dict(state["audit_summary"])
    engine=create_engine(dsn,pool_pre_ping=True,hide_parameters=True); last_id=state["last_resume_id"]
    processed_batch = False
    while True:
        with engine.begin() as conn:
            rows=[int(row[0]) for row in conn.execute(text(
                "SELECT id FROM resume WHERE deleted_at IS NOT NULL AND id>:last ORDER BY id LIMIT :limit"),
                {"last":last_id,"limit":batch_size})]
            if not rows: break
            audit["found"] += len(rows)
            if apply:
                for resume_id in rows:
                    result=conn.execute(text("""INSERT IGNORE INTO target_cleanup_task
                      (operation_id,target_type,target_id,reason,status,next_attempt_at)
                      VALUES(:op,'resume',:id,'legacy_soft_deleted','pending',UTC_TIMESTAMP(6))"""),
                      {"op":str(uuid.uuid5(uuid.NAMESPACE_URL,f"phase11:deleted:resume:{resume_id}")),"id":resume_id})
                    audit["created"] += max(0,result.rowcount or 0)
                last_id=rows[-1]
                audit["last_resume_id"] = last_id
                updated = {"last_resume_id": last_id, "audit_summary": dict(audit)}
                _locked_checkpoint(conn, state, updated)
                state = updated
                processed_batch = True
            else: last_id=rows[-1]
        if apply: _after_checkpoint()
        if len(rows)<batch_size: break
    if apply and not processed_batch:
        # Even an empty run needs a durable canonical cursor and audit digest;
        # the parent runner never trusts evidence present only on stdout.
        with engine.begin() as conn:
            _locked_checkpoint(conn, state, state)
    audit_summary = dict(state["audit_summary"] if apply else {
        "last_resume_id": last_id, "found": audit["found"], "created": audit["created"],
    })
    return {"status":"succeeded","deleted_cursor":state if apply else {
                "last_resume_id": last_id, "audit_summary": audit_summary},
            "found":audit_summary["found"],"created":audit_summary["created"],"audit_summary":audit_summary,
            "dry_run":not apply}


def main() -> int:
    p=argparse.ArgumentParser(description=__doc__); p.add_argument("--dsn",required=True); p.add_argument("--apply",action="store_true")
    p.add_argument("--batch-size",type=int,default=500); p.add_argument("--resume-cursor-json",default="{}"); a=p.parse_args()
    print(json.dumps(reconcile(a.dsn,apply=a.apply,batch_size=a.batch_size,cursor=_cursor(a.resume_cursor_json)),sort_keys=True)); return 0


if __name__ == "__main__":
    raise SystemExit(run_safely(main, "phase11_deleted_target_backfill_failed"))
