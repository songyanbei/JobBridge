"""Resumable Phase 11 reconciliation of legacy ``resume.images``.

The command is dry-run unless ``--apply`` is explicit. Output is aggregate
only; raw IDs, owners, URLs and object keys are never printed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.storage_reference_service import normalize_storage_reference  # noqa: E402
from scripts.phase11_cli_safety import run_safely  # noqa: E402

MIGRATION_KEY = "phase11_resume_media_reconcile"


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")).hexdigest()


def _cursor(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
        if not isinstance(value, dict):
            raise ValueError("resume cursor must be an object")
        last_id = max(0, int(value.get("last_resume_id", 0) or 0))
        scan_last = max(0, int(value.get("scan_last_resume_id", 0) or 0))
        initialized = bool(value.get("scan_initialized", False))
        summary = value.get("audit_summary")
        if summary is None:
            summary = {
                "last_resume_id": last_id, "scan_last_resume_id": scan_last,
                "projection_scanned": 0, "scanned": 0, "created": 0,
                "delete_pending": 0, "issues": 0,
            }
        if not isinstance(summary, dict):
            raise ValueError("invalid audit summary")
        normalized = {
            "last_resume_id": max(0, int(summary.get("last_resume_id", 0) or 0)),
            "scan_last_resume_id": max(0, int(summary.get("scan_last_resume_id", 0) or 0)),
            "projection_scanned": max(0, int(summary.get("projection_scanned", 0) or 0)),
            "scanned": max(0, int(summary.get("scanned", 0) or 0)),
            "created": max(0, int(summary.get("created", 0) or 0)),
            "delete_pending": max(0, int(summary.get("delete_pending", 0) or 0)),
            "issues": max(0, int(summary.get("issues", 0) or 0)),
        }
        if normalized["last_resume_id"] != last_id or normalized["scan_last_resume_id"] != scan_last:
            raise ValueError("cursor audit state mismatch")
        return {"last_resume_id": last_id, "scan_last_resume_id": scan_last,
                "scan_initialized": initialized, "audit_summary": normalized}
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid resume cursor") from exc


def _locked_checkpoint(conn, expected: dict[str, Any], updated: dict[str, Any]) -> None:
    row = conn.execute(text("""SELECT resume_cursor_json,verification_digest
      FROM phase11_migration_ledger WHERE migration_key=:key FOR UPDATE"""),
      {"key": MIGRATION_KEY}).mappings().one()
    raw_cursor = row["resume_cursor_json"]
    stored = _cursor(raw_cursor if isinstance(raw_cursor, str) else json.dumps(raw_cursor or {}))
    if stored != expected:
        raise RuntimeError("python_checkpoint_cursor_drift")
    stored_digest = row["verification_digest"]
    pristine = not stored["scan_initialized"] and not any(stored["audit_summary"].values())
    if (stored_digest is None and not pristine) or (
        stored_digest is not None and stored_digest != _digest(stored["audit_summary"])
    ):
        raise RuntimeError("python_checkpoint_digest_drift")
    conn.execute(text("""UPDATE phase11_migration_ledger
      SET resume_cursor_json=:cursor,verification_digest=:digest
      WHERE migration_key=:key"""), {
        "cursor": json.dumps(updated, separators=(",", ":"), sort_keys=True),
        "digest": _digest(updated["audit_summary"]), "key": MIGRATION_KEY,
    })


def _after_checkpoint() -> None:
    if os.getenv("PHASE11_TEST_CRASH_AFTER_CHECKPOINT") == "1":
        os._exit(86)


def reconcile(dsn: str, *, apply: bool, batch_size: int,
              cursor: int | dict[str, Any], max_rows_per_second: int) -> dict:
    if not 1 <= batch_size <= 5000 or not 1 <= max_rows_per_second <= 10000:
        raise ValueError("batch/rate outside safe range")
    engine = create_engine(dsn, pool_pre_ping=True, hide_parameters=True)
    state = _cursor(json.dumps({"last_resume_id": cursor} if isinstance(cursor, int) else cursor))
    counts = dict(state["audit_summary"])
    last_id = state["last_resume_id"]
    # Pass one is deliberately global and completes before any binding.  The
    # hash-only registry survives batch/process boundaries. Shared references
    # create issues for *both* owners, never just the later row.
    seen: dict[str, int] = {}
    shared_key_hashes: set[str] = set()
    scan_last = state["scan_last_resume_id"]
    if apply and not state["scan_initialized"]:
        # This registry is a materialized hash-only projection of the current
        # resume references, not an append-only discovery log.  Rebuilding it
        # makes a later remediation (detach/de-duplicate) independently
        # verifiable.  A crash leaves an incomplete projection and therefore
        # verify fails closed until this global pass is rerun.
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM phase11_resume_media_key_scan"))
            updated = dict(state)
            updated["scan_initialized"] = True
            _locked_checkpoint(conn, state, updated)
            state = updated
    if apply and scan_last:
        with engine.connect() as conn:
            for row in conn.execute(text("""SELECT key_hash,MIN(resume_id)
              FROM phase11_resume_media_key_scan WHERE reference_kind='valid'
              GROUP BY key_hash""")):
                seen[str(row[0])] = int(row[1])
            shared_key_hashes = {str(row[0]) for row in conn.execute(text("""SELECT key_hash
              FROM phase11_resume_media_key_scan WHERE reference_kind='valid'
              GROUP BY key_hash HAVING COUNT(DISTINCT resume_id)>1"""))}
    while True:
        with engine.begin() as conn:
            scan_rows = conn.execute(text("""SELECT id,images FROM resume
              WHERE id>:last ORDER BY id LIMIT :limit"""),
              {"last": scan_last, "limit": batch_size}).mappings().all()
            if not scan_rows:
                break
            for scan_row in scan_rows:
                raw_images = scan_row["images"]
                registry_counts: dict[tuple[str, str], int] = {}
                if isinstance(raw_images, str):
                    try:
                        raw_images = json.loads(raw_images)
                    except json.JSONDecodeError:
                        raw_images = None
                        if apply:
                            invalid_hash = hashlib.sha256(f"invalid-json:{scan_row['id']}".encode()).hexdigest()
                            registry_counts[(invalid_hash, "invalid")] = 1
                            conn.execute(text("""INSERT IGNORE INTO resume_media_isolation_issue
                              (resume_id,key_hash,issue_type,status)
                              VALUES(:id,:hash,'invalid_json','open')"""),
                              {"id": scan_row["id"], "hash": invalid_hash})
                if raw_images is None:
                    raw_images = []
                if not isinstance(raw_images, list):
                    raw_images = [None]
                for raw in raw_images:
                    try:
                        key = normalize_storage_reference(raw)
                    except (TypeError, ValueError):
                        invalid_hash = hashlib.sha256(
                            ("invalid-reference:" + json.dumps(raw, ensure_ascii=True, sort_keys=True, default=str)).encode()
                        ).hexdigest()
                        registry_counts[(invalid_hash, "invalid")] = registry_counts.get((invalid_hash, "invalid"), 0) + 1
                        if apply:
                            conn.execute(text("""INSERT IGNORE INTO resume_media_isolation_issue
                              (resume_id,key_hash,issue_type,status)
                              VALUES(:id,:hash,'invalid_reference','open')"""),
                              {"id": scan_row["id"], "hash": invalid_hash})
                        continue
                    key_hash = hashlib.sha256(key.encode()).hexdigest()
                    registry_counts[(key_hash, "valid")] = registry_counts.get((key_hash, "valid"), 0) + 1
                    first_id = seen.setdefault(key_hash, int(scan_row["id"]))
                    if first_id != int(scan_row["id"]):
                        shared_key_hashes.add(key_hash)
                        counts["issues"] += 2
                        if apply:
                            for affected_id in (first_id, int(scan_row["id"])):
                                conn.execute(text("""INSERT IGNORE INTO resume_media_isolation_issue
                                  (resume_id,key_hash,issue_type,status)
                                  VALUES(:id,:hash,'shared_reference','open')"""),
                                  {"id": affected_id, "hash": key_hash})
                if apply:
                    for (key_hash, reference_kind), reference_count in registry_counts.items():
                        conn.execute(text("""INSERT INTO phase11_resume_media_key_scan
                          (resume_id,key_hash,reference_kind,reference_count)
                          VALUES(:id,:hash,:kind,:count)
                          ON DUPLICATE KEY UPDATE reference_count=VALUES(reference_count),
                            first_seen_at=LEAST(first_seen_at,VALUES(first_seen_at))"""), {
                              "id": scan_row["id"], "hash": key_hash,
                              "kind": reference_kind, "count": reference_count,
                          })
                        if reference_kind == "valid" and reference_count > 1:
                            conn.execute(text("""INSERT IGNORE INTO resume_media_isolation_issue
                              (resume_id,key_hash,issue_type,status)
                              VALUES(:id,:hash,'duplicate_reference','open')"""),
                              {"id": scan_row["id"], "hash": key_hash})
            scan_last = int(scan_rows[-1]["id"])
            counts["scan_last_resume_id"] = scan_last
            counts["projection_scanned"] += len(scan_rows)
            if apply:
                updated = dict(state)
                updated["scan_last_resume_id"] = scan_last
                updated["audit_summary"] = dict(counts)
                _locked_checkpoint(conn, state, updated)
                state = updated
        if apply: _after_checkpoint()
        if len(scan_rows) < batch_size:
            break
    while True:
        started = time.monotonic()
        with engine.begin() as conn:
            rows = conn.execute(text("SELECT id,owner_userid,images,deleted_at FROM resume WHERE id>:last ORDER BY id LIMIT :limit"),
                                {"last": last_id, "limit": batch_size}).mappings().all()
            if not rows: break
            for row in rows:
                counts["scanned"] += 1
                values = row["images"]
                if isinstance(values, str):
                    try: values = json.loads(values)
                    except json.JSONDecodeError: values = None
                if values is None: values = []
                if not isinstance(values, list): values = [None]
                local: set[str] = set()
                for raw in values:
                    issue_type = None
                    try: key = normalize_storage_reference(raw)
                    except (TypeError, ValueError):
                        key = None; issue_type = "invalid_reference"
                    key_hash = hashlib.sha256((key or f"invalid:{type(raw).__name__}").encode()).hexdigest()
                    shared_issue_exists = False
                    if key is not None and apply:
                        shared_issue_exists = conn.execute(text("""SELECT EXISTS(
                          SELECT 1 FROM resume_media_isolation_issue
                          WHERE key_hash=:hash AND issue_type='shared_reference'
                            AND status<>'resolved')"""), {"hash": key_hash}).scalar_one() == 1
                    # The scan pass is global, whereas this binding pass is
                    # resumable and batched.  Therefore ownership must never
                    # be inferred from which resume happens to bind first.
                    if key is not None and (key_hash in shared_key_hashes or shared_issue_exists):
                        continue
                    if key is not None and key in local:
                        issue_type = "duplicate_reference"
                    elif key is not None:
                        first_resume_id = seen.get(key_hash)
                        if apply:
                            first_resume_id = int(conn.execute(text(
                                "SELECT MIN(resume_id) FROM phase11_resume_media_key_scan "
                                "WHERE key_hash=:hash AND reference_kind='valid' FOR UPDATE"
                            ), {"hash": key_hash}).scalar_one())
                        issue_type = (
                            "shared_reference"
                            if first_resume_id is not None
                            and first_resume_id != int(row["id"])
                            else None
                        )
                    if issue_type:
                        counts["issues"] += 1
                        if apply:
                            conn.execute(text("""INSERT IGNORE INTO resume_media_isolation_issue
                              (resume_id,key_hash,issue_type,status) VALUES (:id,:hash,:kind,'open')"""),
                              {"id": row["id"], "hash": key_hash, "kind": issue_type})
                        continue
                    local.add(key); seen.setdefault(key_hash, int(row["id"]))
                    existing = conn.execute(text("SELECT owner_userid,entity_type,entity_id,state FROM media_asset_lifecycle WHERE object_key=:key FOR UPDATE"), {"key": key}).mappings().first()
                    if existing and (existing["owner_userid"] != row["owner_userid"] or existing["entity_type"] not in (None, "resume") or existing["entity_id"] not in (None, row["id"])):
                        counts["issues"] += 1
                        if apply:
                            conn.execute(text("INSERT IGNORE INTO resume_media_isolation_issue(resume_id,key_hash,issue_type,status) VALUES(:id,:hash,'owner_binding_conflict','open')"), {"id": row["id"], "hash": key_hash})
                        continue
                    if existing and existing["entity_type"] is None and apply:
                        desired_state = "delete_pending" if row["deleted_at"] else "attached"
                        conn.execute(text("""UPDATE media_asset_lifecycle
                          SET entity_type='resume',entity_id=:id,state=:state,
                              next_attempt_at=IF(:state='delete_pending',UTC_TIMESTAMP(6),NULL)
                          WHERE object_key=:key AND entity_type IS NULL AND entity_id IS NULL"""),
                          {"key": key, "id": row["id"], "state": desired_state})
                    elif existing and apply:
                        desired = "delete_pending" if row["deleted_at"] else "attached"
                        safe_repair = (
                            existing["state"] in {"pending", desired}
                            or (row["deleted_at"] and existing["state"] == "attached")
                        )
                        if safe_repair and existing["state"] != desired:
                            conn.execute(text("""UPDATE media_asset_lifecycle SET state=:state,
                              next_attempt_at=IF(:state='delete_pending',UTC_TIMESTAMP(6),NULL)
                              WHERE object_key=:key"""), {"state": desired, "key": key})
                        elif not safe_repair:
                            counts["issues"] += 1
                            conn.execute(text("""INSERT IGNORE INTO resume_media_isolation_issue
                              (resume_id,key_hash,issue_type,status)
                              VALUES(:id,:hash,'illegal_lifecycle_state','open')"""),
                              {"id": row["id"], "hash": key_hash})
                    if not existing and apply:
                        desired_state = "delete_pending" if row["deleted_at"] else "attached"
                        conn.execute(text("""INSERT INTO media_asset_lifecycle
                          (object_key,operation_id,owner_userid,entity_type,entity_id,state,next_attempt_at)
                          VALUES (:key,:op,:owner,'resume',:id,:state,IF(:state='delete_pending',UTC_TIMESTAMP(6),NULL))"""),
                          {"key": key, "op": str(uuid.uuid4()), "owner": row["owner_userid"], "id": row["id"], "state": desired_state})
                        counts["created"] += 1
                        counts["delete_pending"] += int(desired_state == "delete_pending")
                last_id = int(row["id"])
            counts["last_resume_id"] = last_id
            if apply:
                updated = dict(state)
                updated["last_resume_id"] = last_id
                updated["audit_summary"] = dict(counts)
                _locked_checkpoint(conn, state, updated)
                state = updated
        if apply: _after_checkpoint()
        elapsed = time.monotonic() - started
        target = len(rows) / max_rows_per_second
        if elapsed < target: time.sleep(target - elapsed)
    audit_summary = dict(state["audit_summary"] if apply else counts)
    audit_summary["last_resume_id"] = last_id
    audit_summary["scan_last_resume_id"] = scan_last
    result_cursor = state if apply else {
        "last_resume_id": last_id, "scan_last_resume_id": scan_last,
        "scan_initialized": False, "audit_summary": audit_summary,
    }
    return {"status": "succeeded", "resume_cursor": result_cursor,
            **{key: audit_summary[key] for key in ("scanned", "created", "delete_pending", "issues")},
            "audit_summary": audit_summary, "dry_run": not apply}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True); parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-size", type=int, default=500); parser.add_argument("--max-rows-per-second", type=int, default=1000)
    parser.add_argument("--resume-cursor-json", default="{}")
    args = parser.parse_args()
    print(json.dumps(reconcile(args.dsn, apply=args.apply, batch_size=args.batch_size,
                               cursor=_cursor(args.resume_cursor_json), max_rows_per_second=args.max_rows_per_second), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_safely(main, "phase11_media_reconcile_failed"))
