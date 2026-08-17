"""Keyset/resumable lifecycle backfill bounded by the runner cutover id."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import bindparam, create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.phase11_cli_safety import run_safely

MIGRATION_KEY = "phase11_resume_lifecycle_backfill"
NON_BUSINESS_COLUMNS = frozenset({
    # Row identity / optimistic-lock plumbing.
    "id", "version", "created_at", "updated_at",
    # Review and lifecycle state.  The backfill is explicitly allowed to
    # populate lifecycle state while every content-bearing column is frozen.
    "audit_status", "audit_reason", "audited_by", "audited_at",
    "activated_at", "candidate_expires_at", "expires_at", "delist_reason",
    "deleted_at",
})


def _canonical(value: Any) -> Any:
    """Type-tag values so SQL NULL, JSON null, text and numbers never alias."""
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, (Decimal, float)):
        return ["number", str(value)]
    if isinstance(value, str):
        return ["text", value]
    if isinstance(value, bytes):
        return ["bytes", value.hex()]
    if isinstance(value, dict):
        return ["object", [[str(key), _canonical(value[key])] for key in sorted(value)]]
    if isinstance(value, (list, tuple)):
        return ["array", [_canonical(item) for item in value]]
    return [type(value).__name__, str(value)]


def _business_shape(conn) -> tuple[list[str], frozenset[str]]:
    rows = conn.execute(text("""SELECT COLUMN_NAME,DATA_TYPE
      FROM information_schema.columns
      WHERE table_schema=DATABASE() AND table_name='resume'
      ORDER BY ORDINAL_POSITION""")).all()
    columns = [str(row[0]) for row in rows if str(row[0]) not in NON_BUSINESS_COLUMNS]
    if not columns:
        raise RuntimeError("resume_business_columns_missing")
    return columns, frozenset(str(row[0]) for row in rows if str(row[1]).lower() == "json")


def _business_digests(conn, ids: list[int], columns: list[str], json_columns: frozenset[str]) -> dict[int, str]:
    quoted = ",".join(f"`{column}`" for column in columns)
    rows = conn.execute(text(f"SELECT id,{quoted} FROM resume WHERE id IN :ids").bindparams(
        bindparam("ids", expanding=True)
    ), {"ids": ids}).mappings().all()
    result: dict[int, str] = {}
    for row in rows:
        payload = []
        for column in columns:
            value = row[column]
            if column in json_columns and isinstance(value, str):
                value = json.loads(value)
            payload.append([column, _canonical(value)])
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        result[int(row["id"])] = hashlib.sha256(encoded).hexdigest()
    return result


def _cursor(raw: str) -> dict[str, Any]:
    value = json.loads(raw or "{}")
    if not isinstance(value, dict):
        raise ValueError("resume cursor must be an object")
    digest = value.get("business_verification_digest", "0" * 64)
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("invalid business verification digest")
    summary = value.get("audit_summary")
    if summary is not None and not isinstance(summary, dict):
        raise ValueError("invalid audit summary")
    ttl_evidence = value.get("candidate_ttl_evidence")
    if ttl_evidence is not None:
        if not isinstance(ttl_evidence, dict) or set(ttl_evidence) != {
            "config_value", "value_type", "updated_at", "updated_by", "days", "revision",
        }:
            raise ValueError("invalid candidate TTL evidence")
        if (
            ttl_evidence["value_type"] != "int"
            or not isinstance(ttl_evidence["config_value"], str)
            or not re.fullmatch(r"[0-9]+", ttl_evidence["config_value"])
            or int(ttl_evidence["config_value"]) != ttl_evidence["days"]
            or not 1 <= ttl_evidence["days"] <= 365
            or str(ttl_evidence["days"]) != ttl_evidence["config_value"]
            or not isinstance(ttl_evidence["updated_at"], str)
            or not isinstance(ttl_evidence["revision"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", ttl_evidence["revision"])
        ):
            raise ValueError("invalid candidate TTL evidence")
        proof = {key: ttl_evidence[key] for key in ttl_evidence if key != "revision"}
        if hashlib.sha256(json.dumps(
            proof, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        ).encode("utf-8")).hexdigest() != ttl_evidence["revision"]:
            raise ValueError("invalid candidate TTL evidence revision")
    state = {
        "last_resume_id": max(0, int(value.get("last_resume_id", 0) or 0)),
        "verified_business_rows": max(0, int(value.get("verified_business_rows", 0) or 0)),
        "business_verification_digest": digest,
        "scanned": max(0, int(value.get("scanned", 0) or 0)),
        "changed": max(0, int(value.get("changed", 0) or 0)),
        "candidate_ttl_evidence": ttl_evidence,
    }
    if summary is None:
        summary = {
            "cutover_resume_id": max(0, int(value.get("cutover_resume_id", 0) or 0)),
            "last_resume_id": state["last_resume_id"],
            "bounded_row_count": max(0, int(value.get("bounded_row_count", 0) or 0)),
            "verified_business_rows": state["verified_business_rows"],
            "business_verification_digest": state["business_verification_digest"],
            "scanned": state["scanned"], "changed": state["changed"],
            "lifecycle_status_counts": value.get("lifecycle_status_counts", []),
            "candidate_ttl_evidence": ttl_evidence,
        }
    normalized_summary = {
        "cutover_resume_id": max(0, int(summary.get("cutover_resume_id", 0) or 0)),
        "last_resume_id": max(0, int(summary.get("last_resume_id", 0) or 0)),
        "bounded_row_count": max(0, int(summary.get("bounded_row_count", 0) or 0)),
        "verified_business_rows": max(0, int(summary.get("verified_business_rows", 0) or 0)),
        "business_verification_digest": str(summary.get(
            "business_verification_digest", "0" * 64,
        )),
        "scanned": max(0, int(summary.get("scanned", 0) or 0)),
        "changed": max(0, int(summary.get("changed", 0) or 0)),
        "lifecycle_status_counts": summary.get("lifecycle_status_counts", []),
        "candidate_ttl_evidence": summary.get("candidate_ttl_evidence"),
    }
    if not re.fullmatch(r"[0-9a-f]{64}", normalized_summary["business_verification_digest"]):
        raise ValueError("invalid business verification digest")
    if not isinstance(normalized_summary["lifecycle_status_counts"], list):
        raise ValueError("invalid lifecycle status counts")
    for key in ("last_resume_id", "verified_business_rows", "business_verification_digest",
                "scanned", "changed", "candidate_ttl_evidence"):
        if normalized_summary[key] != state[key]:
            raise ValueError("cursor audit state mismatch")
    state["audit_summary"] = normalized_summary
    return state


def _locked_candidate_ttl_evidence(conn) -> dict[str, Any]:
    """Lock and return the unique canonical candidate TTL row."""
    rows = conn.execute(text("""SELECT config_value,value_type,updated_at,updated_by
      FROM system_config WHERE config_key='ttl.resume.candidate.days'
      FOR UPDATE""")).mappings().all()
    if len(rows) != 1:
        raise RuntimeError("candidate_ttl_config_invalid")
    row = rows[0]
    raw = row["config_value"]
    if (
        row["value_type"] != "int"
        or not isinstance(raw, str)
        or not re.fullmatch(r"[0-9]+", raw)
    ):
        raise RuntimeError("candidate_ttl_config_invalid")
    days = int(raw)
    if not 1 <= days <= 365 or str(days) != raw:
        raise RuntimeError("candidate_ttl_config_invalid")
    updated_at = row["updated_at"]
    proof = {
        "config_value": raw,
        "value_type": "int",
        "updated_at": updated_at.isoformat(timespec="microseconds") if updated_at else "",
        "updated_by": row["updated_by"],
        "days": days,
    }
    return {
        **proof,
        "revision": hashlib.sha256(json.dumps(
            proof, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        ).encode("utf-8")).hexdigest(),
    }


def _canonical_digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")).hexdigest()


def _locked_checkpoint(conn, expected: dict[str, Any], updated: dict[str, Any]) -> None:
    row = conn.execute(text("""SELECT resume_cursor_json,verification_digest
      FROM phase11_migration_ledger WHERE migration_key=:key FOR UPDATE"""),
      {"key": MIGRATION_KEY}).mappings().one()
    raw_cursor = row["resume_cursor_json"]
    stored = _cursor(raw_cursor if isinstance(raw_cursor, str) else json.dumps(raw_cursor or {}))
    if stored != expected:
        raise RuntimeError("python_checkpoint_cursor_drift")
    stored_digest = row["verification_digest"]
    pristine = (
        stored["last_resume_id"] == 0 and stored["verified_business_rows"] == 0
        and stored["scanned"] == 0 and stored["changed"] == 0
        and stored["business_verification_digest"] == "0" * 64
    )
    if (stored_digest is None and not pristine) or (
        stored_digest is not None
        and stored_digest != _canonical_digest(stored["audit_summary"])
    ):
        raise RuntimeError("python_checkpoint_digest_drift")
    conn.execute(text("""UPDATE phase11_migration_ledger
      SET resume_cursor_json=:cursor,verification_digest=:digest
      WHERE migration_key=:key"""), {
        "cursor": json.dumps(updated, separators=(",", ":"), sort_keys=True),
        "digest": _canonical_digest(updated["audit_summary"]), "key": MIGRATION_KEY,
    })


def _after_checkpoint() -> None:
    if os.getenv("PHASE11_TEST_CRASH_AFTER_CHECKPOINT") == "1":
        os._exit(86)


def backfill(dsn: str, *, apply: bool, batch_size: int, cursor: int | dict[str, Any]) -> dict:
    if not 1 <= batch_size <= 5000:
        raise ValueError("batch_size must be 1..5000")
    engine = create_engine(dsn, pool_pre_ping=True, hide_parameters=True)
    cursor_state = (
        _cursor(json.dumps({"last_resume_id": cursor}))
        if isinstance(cursor, int) else _cursor(json.dumps(cursor))
    )
    last_id = max(0, int(cursor_state.get("last_resume_id", 0) or 0))
    verified_business_rows = max(0, int(cursor_state.get("verified_business_rows", 0) or 0))
    business_digest = str(cursor_state.get("business_verification_digest", "0" * 64))
    if not re.fullmatch(r"[0-9a-f]{64}", business_digest):
        raise ValueError("invalid business verification digest")
    scanned = int(cursor_state["scanned"])
    changed = int(cursor_state["changed"])
    processed_batch = False
    with engine.begin() as conn:
        ledger = conn.execute(text("""SELECT cutover_resume_id,started_at FROM phase11_migration_ledger
          WHERE migration_key=:key"""), {"key": MIGRATION_KEY}).mappings().one()
        cutover = int(ledger["cutover_resume_id"])
        started_at = ledger["started_at"]
        current_ttl_evidence = _locked_candidate_ttl_evidence(conn)
        frozen_ttl_evidence = cursor_state.get("candidate_ttl_evidence")
        if frozen_ttl_evidence is None:
            if last_id or scanned or changed or verified_business_rows:
                raise RuntimeError("candidate_ttl_evidence_missing")
            frozen_ttl_evidence = current_ttl_evidence
        elif frozen_ttl_evidence != current_ttl_evidence:
            raise RuntimeError("candidate_ttl_config_drift")
        business_columns, json_columns = _business_shape(conn)
    while True:
        with engine.begin() as conn:
            current_ttl_evidence = _locked_candidate_ttl_evidence(conn)
            if current_ttl_evidence != frozen_ttl_evidence:
                raise RuntimeError("candidate_ttl_config_drift")
            ttl = int(frozen_ttl_evidence["days"])
            rows = conn.execute(text("""SELECT id,version,updated_at
              FROM resume WHERE id>:last AND id<=:cutover ORDER BY id LIMIT :limit FOR UPDATE"""),
              {"last": last_id, "cutover": cutover, "limit": batch_size}).mappings().all()
            if not rows:
                break
            before_business = _business_digests(conn, ids=[int(row["id"]) for row in rows],
                                                columns=business_columns, json_columns=json_columns)
            before = {int(row["id"]): (int(row["version"]), row["updated_at"], before_business[int(row["id"])]) for row in rows}
            ids = list(before)
            if apply:
                conn.execute(text("""INSERT INTO phase11_resume_lifecycle_backup
                  (resume_id,expires_at,activated_at,candidate_expires_at,deleted_at,version,updated_at)
                  SELECT id,expires_at,activated_at,candidate_expires_at,deleted_at,version,updated_at
                  FROM resume WHERE id IN :ids
                  ON DUPLICATE KEY UPDATE resume_id=VALUES(resume_id)""").bindparams(
                    bindparam("ids", expanding=True)), {"ids": ids})
                result = conn.execute(text("""UPDATE resume SET
                  activated_at=CASE WHEN deleted_at IS NULL AND audit_status='passed'
                    THEN COALESCE(audited_at,created_at) ELSE NULL END,
                  expires_at=CASE WHEN deleted_at IS NULL AND audit_status IN ('pending','rejected')
                    THEN NULL ELSE expires_at END,
                  candidate_expires_at=CASE WHEN deleted_at IS NULL AND audit_status IN ('pending','rejected')
                    THEN :started + INTERVAL :ttl DAY ELSE NULL END,
                  version=version,updated_at=updated_at
                  WHERE id IN :ids""").bindparams(bindparam("ids", expanding=True)),
                  {"started": started_at, "ttl": ttl, "ids": ids})
                changed += max(0, result.rowcount or 0)
                after_rows = conn.execute(text("""SELECT id,version,updated_at
                  FROM resume WHERE id IN :ids""").bindparams(bindparam("ids", expanding=True)),
                  {"ids": ids}).mappings().all()
                after_business = _business_digests(conn, ids=ids, columns=business_columns,
                                                   json_columns=json_columns)
                after = {int(row["id"]): (int(row["version"]), row["updated_at"], after_business[int(row["id"])]) for row in after_rows}
                if len(after) != len(before) or after != before:
                    raise RuntimeError("backfill_business_fields_changed")
                batch_evidence = [[resume_id, before[resume_id][2]] for resume_id in sorted(before)]
                business_digest = hashlib.sha256(
                    bytes.fromhex(business_digest)
                    + json.dumps(batch_evidence, separators=(",", ":")).encode("utf-8")
                ).hexdigest()
                verified_business_rows += len(batch_evidence)
                anomalies = int(conn.execute(text("""SELECT COUNT(*) FROM resume WHERE id IN :ids AND
                  ((deleted_at IS NULL AND audit_status='passed' AND
                    (activated_at IS NULL OR expires_at IS NULL OR candidate_expires_at IS NOT NULL)) OR
                   (deleted_at IS NULL AND audit_status IN ('pending','rejected') AND
                    (activated_at IS NOT NULL OR expires_at IS NOT NULL OR candidate_expires_at IS NULL)))""").bindparams(
                      bindparam("ids", expanding=True)), {"ids": ids}).scalar_one())
                if anomalies:
                    raise RuntimeError("backfill_lifecycle_verification_failed")
            scanned += len(rows)
            last_id = int(rows[-1]["id"])
            if apply:
                status_rows = conn.execute(text("""SELECT audit_status,deleted_at IS NULL AS is_live,COUNT(*)
                  FROM resume WHERE id<=:cutover GROUP BY audit_status,is_live
                  ORDER BY audit_status,is_live"""), {"cutover": cutover}).all()
                bounded_row_count = int(conn.execute(text(
                    "SELECT COUNT(*) FROM resume WHERE id<=:cutover"
                ), {"cutover": cutover}).scalar_one())
                audit_summary = {
                    "cutover_resume_id": cutover, "last_resume_id": last_id,
                    "bounded_row_count": bounded_row_count,
                    "verified_business_rows": verified_business_rows,
                    "business_verification_digest": business_digest,
                    "candidate_ttl_evidence": frozen_ttl_evidence,
                    "scanned": scanned, "changed": changed,
                    "lifecycle_status_counts": [
                        {"audit_status": str(row[0]), "is_live": bool(row[1]), "count": int(row[2])}
                        for row in status_rows
                    ],
                }
                updated = {
                    "last_resume_id": last_id,
                    "verified_business_rows": verified_business_rows,
                    "business_verification_digest": business_digest,
                    "candidate_ttl_evidence": frozen_ttl_evidence,
                    "scanned": scanned, "changed": changed,
                    "audit_summary": audit_summary,
                }
                _locked_checkpoint(conn, cursor_state, updated)
                cursor_state = updated
                processed_batch = True
        if apply: _after_checkpoint()
        if len(rows) < batch_size:
            break
    with engine.connect() as conn:
        status_rows = conn.execute(text("""SELECT audit_status,deleted_at IS NULL AS is_live,COUNT(*)
          FROM resume WHERE id<=:cutover GROUP BY audit_status,is_live
          ORDER BY audit_status,is_live"""), {"cutover": cutover}).all()
        bounded_row_count = int(conn.execute(text(
            "SELECT COUNT(*) FROM resume WHERE id<=:cutover"
        ), {"cutover": cutover}).scalar_one())
    audit_summary = {
        "cutover_resume_id": cutover,
        "last_resume_id": last_id,
        "bounded_row_count": bounded_row_count,
        "verified_business_rows": verified_business_rows,
        "business_verification_digest": business_digest,
        "candidate_ttl_evidence": frozen_ttl_evidence,
        "scanned": scanned, "changed": changed,
        "lifecycle_status_counts": [
            {"audit_status": str(row[0]), "is_live": bool(row[1]), "count": int(row[2])}
            for row in status_rows
        ],
    }
    if apply and not processed_batch:
        updated = {
            "last_resume_id": last_id,
            "verified_business_rows": verified_business_rows,
            "business_verification_digest": business_digest,
            "candidate_ttl_evidence": frozen_ttl_evidence,
            "scanned": scanned, "changed": changed,
            "audit_summary": audit_summary,
        }
        if updated != cursor_state or not cursor_state.get("audit_summary"):
            with engine.begin() as conn:
                _locked_checkpoint(conn, cursor_state, updated)
            cursor_state = updated
    elif apply:
        audit_summary = cursor_state["audit_summary"]
    else:
        cursor_state = {
            "last_resume_id": last_id, "verified_business_rows": verified_business_rows,
            "business_verification_digest": business_digest, "scanned": scanned,
            "changed": changed, "candidate_ttl_evidence": frozen_ttl_evidence,
            "audit_summary": audit_summary,
        }
    summary = {"cutover_resume_id": cutover, "last_resume_id": last_id,
               "scanned": audit_summary["scanned"], "changed": audit_summary["changed"]}
    summary["summary_digest"] = hashlib.sha256(
        json.dumps(audit_summary, sort_keys=True).encode()
    ).hexdigest()
    return {"status": "succeeded", "lifecycle_cursor": cursor_state,
            **summary, "audit_summary": audit_summary, "dry_run": not apply}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True); parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-size", type=int, default=500); parser.add_argument("--resume-cursor-json", default="{}")
    args = parser.parse_args()
    print(json.dumps(backfill(args.dsn, apply=args.apply, batch_size=args.batch_size,
                              cursor=_cursor(args.resume_cursor_json)), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_safely(main, "phase11_lifecycle_backfill_failed"))
