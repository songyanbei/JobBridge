"""Create cleanup tasks and Redis fences for persisted orphan resume targets."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import redis
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.phase11_cli_safety import run_safely  # noqa: E402
from app.core.redis_client import (  # noqa: E402
    RECOMMENDATION_SESSION_TARGET_INDEX_PREFIX,
    RECOMMENDATION_SESSION_REVOCATION_FENCE_KEY,
    fence_recommendation_session_indexes,
    validate_redis_durability_policy,
)

MIGRATION_KEY = "phase11_resume_orphan_target_reconcile"


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
        summary = {"last_target_id": max(0, int(value.get("last_target_id", 0) or 0)),
                   "found": 0, "created": 0}
    if not isinstance(summary, dict):
        raise ValueError("invalid audit summary")
    normalized = {
        "last_target_id": max(0, int(summary.get("last_target_id", 0) or 0)),
        "found": max(0, int(summary.get("found", 0) or 0)),
        "created": max(0, int(summary.get("created", 0) or 0)),
    }
    if normalized["last_target_id"] != max(
        0, int(value.get("last_target_id", normalized["last_target_id"]) or 0)
    ):
        raise ValueError("cursor audit state mismatch")
    return {"last_target_id": normalized["last_target_id"], "audit_summary": normalized}


def _locked_checkpoint(conn, expected: dict[str, Any], updated: dict[str, Any]) -> None:
    row = conn.execute(text("""SELECT resume_cursor_json,verification_digest
      FROM phase11_migration_ledger WHERE migration_key=:key FOR UPDATE"""),
      {"key": MIGRATION_KEY}).mappings().one()
    raw_cursor = row["resume_cursor_json"]
    stored = _cursor(raw_cursor if isinstance(raw_cursor, str) else json.dumps(raw_cursor or {}))
    if stored != expected:
        raise RuntimeError("python_checkpoint_cursor_drift")
    stored_digest = row["verification_digest"]
    pristine = stored["audit_summary"] == {"last_target_id": 0, "found": 0, "created": 0}
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


def _namespaced(namespace: str, key: str) -> str:
    return f"{namespace}:{key}" if namespace else key


def reconcile(dsn: str, *, redis_dsn: str, redis_namespace: str,
              apply: bool, batch_size: int, cursor: int | dict[str, Any]) -> dict:
    if not 1 <= batch_size <= 5000: raise ValueError("batch_size must be 1..5000")
    if not redis_dsn or not redis_namespace or any(ch.isspace() for ch in redis_namespace):
        raise ValueError("explicit redis_dsn and redis_namespace are required")
    engine = create_engine(dsn, pool_pre_ping=True, hide_parameters=True)
    redis_client = redis.Redis.from_url(redis_dsn, decode_responses=True)
    # A fence is a correctness primitive, so an ephemeral/evicting Redis is
    # rejected before the script obtains its migration lock or writes MySQL.
    validate_redis_durability_policy(redis_client)
    redis_client.ping()
    state = _cursor(json.dumps({"last_target_id": cursor} if isinstance(cursor, int) else cursor))
    audit = dict(state["audit_summary"])
    last_id = state["last_target_id"]
    processed_batch = False
    # Enumerate every durable target fact.  Do not infer that request rows are
    # a superset: delivery context, conversation/outbox attribution, daily
    # aggregates and click logs can outlive or diverge from request JSON.
    source = """SELECT DISTINCT target_id FROM (
      SELECT target_id FROM recommendation_impression WHERE target_type='resume'
      UNION ALL
      SELECT j.target_id FROM recommendation_request q
      JOIN JSON_TABLE(q.served_top_ids, '$[*]'
        COLUMNS(target_id BIGINT PATH '$' NULL ON ERROR)) j
      WHERE q.direction='search_worker'
      UNION ALL
      SELECT j.target_id FROM recommendation_request q
      JOIN JSON_TABLE(COALESCE(q.shadow_top_ids,JSON_ARRAY()), '$[*]'
        COLUMNS(target_id BIGINT PATH '$' NULL ON ERROR)) j
      WHERE q.direction='search_worker'
      UNION ALL
      SELECT j.target_id FROM recommendation_search_attempt a
      JOIN recommendation_request q ON q.request_id=a.request_id AND q.direction='search_worker'
      JOIN JSON_TABLE(a.candidate_ids, '$[*]'
        COLUMNS(target_id BIGINT PATH '$' NULL ON ERROR)) j
      UNION ALL
      SELECT j.target_id FROM recommendation_search_attempt a
      JOIN recommendation_request q ON q.request_id=a.request_id AND q.direction='search_worker'
      JOIN JSON_TABLE(a.precision_pool_ids, '$[*]'
        COLUMNS(target_id BIGINT PATH '$' NULL ON ERROR)) j
      UNION ALL
      SELECT j.target_id FROM recommendation_delivery d
      JOIN JSON_TABLE(d.recommendation_context, '$.items[*]'
        COLUMNS(target_type VARCHAR(16) PATH '$.target_type' NULL ON ERROR,
                target_id BIGINT PATH '$.target_id' NULL ON ERROR)) j
      WHERE j.target_type='resume'
      UNION ALL
      SELECT j.target_id FROM conversation_log c
      JOIN recommendation_delivery d ON d.delivery_id=c.recommendation_delivery_id
      JOIN JSON_TABLE(d.recommendation_context, '$.items[*]'
        COLUMNS(target_type VARCHAR(16) PATH '$.target_type' NULL ON ERROR,
                target_id BIGINT PATH '$.target_id' NULL ON ERROR)) j
      WHERE j.target_type='resume'
      UNION ALL
      SELECT j.target_id FROM wecom_outbound_outbox o
      JOIN recommendation_delivery d ON d.delivery_id=o.recommendation_delivery_id
      JOIN JSON_TABLE(d.recommendation_context, '$.items[*]'
        COLUMNS(target_type VARCHAR(16) PATH '$.target_type' NULL ON ERROR,
                target_id BIGINT PATH '$.target_id' NULL ON ERROR)) j
      WHERE j.target_type='resume'
      UNION ALL
      SELECT target_id FROM recommendation_exposure_daily WHERE target_type='resume'
      UNION ALL
      SELECT target_id FROM event_log WHERE target_type='resume'
    ) targets LEFT JOIN resume r ON r.id=targets.target_id
    WHERE r.id IS NULL AND target_id IS NOT NULL AND target_id>:last
    ORDER BY target_id LIMIT :limit"""
    lock_conn = engine.connect()
    try:
        if int(lock_conn.execute(text(
            "SELECT GET_LOCK('phase11_resume_orphan_target_reconcile',10)"
        )).scalar() or 0) != 1:
            raise RuntimeError("phase11_orphan_reconcile_lock_unavailable")
        while True:
          with engine.begin() as conn:
            rows = [int(row[0]) for row in conn.execute(text(source), {"last": last_id, "limit": batch_size})]
            if not rows:
                break
            audit["found"] += len(rows)
            if apply:
                for target_id in rows:
                    result = conn.execute(text("""INSERT IGNORE INTO target_cleanup_task
                      (operation_id,target_type,target_id,reason,status,next_attempt_at)
                      VALUES (:op,'resume',:id,'legacy_orphan','pending',UTC_TIMESTAMP(6))"""),
                      {"op": str(uuid.uuid5(uuid.NAMESPACE_URL, f"phase11:orphan:resume:{target_id}")), "id": target_id})
                    audit["created"] += max(0, result.rowcount or 0)
                index_keys = [
                    _namespaced(
                        redis_namespace,
                        f"{RECOMMENDATION_SESSION_TARGET_INDEX_PREFIX}resume:{target_id}",
                    )
                    for target_id in rows
                ]
                fence_recommendation_session_indexes(
                    index_keys,
                    client=redis_client,
                    fence_key=_namespaced(
                        redis_namespace,
                        RECOMMENDATION_SESSION_REVOCATION_FENCE_KEY,
                    ),
                )
                last_id = rows[-1]
                audit["last_target_id"] = last_id
                updated = {"last_target_id": last_id, "audit_summary": dict(audit)}
                _locked_checkpoint(conn, state, updated)
                state = updated
                processed_batch = True
            else: last_id = rows[-1]
          if apply: _after_checkpoint()
          if len(rows) < batch_size: break
    finally:
        try:
            lock_conn.execute(text(
                "SELECT RELEASE_LOCK('phase11_resume_orphan_target_reconcile')"
            ))
        finally:
            lock_conn.close()
    if apply and not processed_batch:
        # Canonicalize an empty/legacy cursor and attach its audit digest too.
        # Without this checkpoint an empty run would return evidence that was
        # never durably attested by the ledger.
        with engine.begin() as conn:
            _locked_checkpoint(conn, state, state)
    audit_summary = dict(state["audit_summary"] if apply else {
        "last_target_id": last_id, "found": audit["found"], "created": audit["created"],
    })
    return {"status": "succeeded", "orphan_cursor": state if apply else {
                "last_target_id": last_id, "audit_summary": audit_summary},
            "found": audit_summary["found"], "created": audit_summary["created"], "audit_summary": audit_summary,
            "dry_run": not apply}


def main() -> int:
    p=argparse.ArgumentParser(description=__doc__); p.add_argument("--dsn",required=True); p.add_argument("--apply",action="store_true")
    p.add_argument("--redis-dsn",required=True); p.add_argument("--redis-namespace",required=True)
    p.add_argument("--batch-size",type=int,default=500); p.add_argument("--resume-cursor-json",default="{}"); a=p.parse_args()
    print(json.dumps(reconcile(a.dsn,redis_dsn=a.redis_dsn,redis_namespace=a.redis_namespace,
                               apply=a.apply,batch_size=a.batch_size,cursor=_cursor(a.resume_cursor_json)),sort_keys=True)); return 0


if __name__ == "__main__":
    raise SystemExit(run_safely(main, "phase11_orphan_reconcile_failed"))
