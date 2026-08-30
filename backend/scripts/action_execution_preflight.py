"""Read-only Workstream C1 Action execution rollout gate.

The command is safe to run before every rollout step.  It validates the
fail-closed mode/percentage contract and, when a database is reachable,
checks the current Action table for stale leases, missing result references,
and retryable replay backlog.  It never mutates Action, session, or outbox
rows; a non-zero exit means the caller must keep routing on legacy.

Examples::

    cd backend
    python scripts/action_execution_preflight.py --dsn-env DB_URL
    ACTION_EXECUTION_MODE=shadow python scripts/action_execution_preflight.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import create_engine, text


ERROR = "error"
WARNING = "warning"
INFO = "info"
REQUIRED_COLUMNS = {
    "turn_id",
    "action_name",
    "status",
    "request_digest",
    "result_digest",
    "lease_until",
}


@dataclass
class Finding:
    level: str
    code: str
    message: str
    details: dict[str, Any] | None = None


def _env_int(name: str, default: int, findings: list[Finding], *, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        findings.append(Finding(ERROR, "invalid_integer", f"{name}={raw!r} is not an integer"))
        return default
    if value < minimum:
        findings.append(Finding(ERROR, "invalid_threshold", f"{name}={value} must be >= {minimum}"))
    return value


def check_runtime_config(findings: list[Finding]) -> dict[str, Any]:
    mode = os.environ.get("ACTION_EXECUTION_MODE", "off").strip().lower()
    rollout = _env_int("ACTION_EXECUTION_ROLLOUT_PERCENTAGE", 0, findings)
    if mode not in {"off", "shadow", "on"}:
        findings.append(Finding(ERROR, "invalid_action_mode", f"ACTION_EXECUTION_MODE={mode!r} is invalid"))
    if rollout > 100:
        findings.append(Finding(ERROR, "invalid_rollout_percentage", f"ACTION_EXECUTION_ROLLOUT_PERCENTAGE={rollout} exceeds 100"))
    if mode == "off" and rollout:
        findings.append(Finding(ERROR, "off_with_nonzero_rollout", "mode=off must have rollout percentage 0"))
    if mode == "on" and rollout == 0:
        findings.append(Finding(INFO, "on_with_zero_rollout", "mode=on but no users are assigned; legacy remains served"))
    return {
        "mode": mode,
        "rollout_percentage": rollout,
        "lease_seconds": _env_int("ACTION_EXECUTION_LEASE_SECONDS", 180, findings, minimum=1),
        "replay_max_attempts": _env_int("ACTION_REPLAY_MAX_ATTEMPTS", 5, findings, minimum=1),
        "replay_stale_seconds": _env_int("ACTION_REPLAY_STALE_SECONDS", 3600, findings, minimum=1),
    }


def check_database(dsn: str, findings: list[Finding]) -> dict[str, Any]:
    result: dict[str, Any] = {"reachable": False}
    engine = create_engine(dsn, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            result["reachable"] = True
            table_exists = bool(conn.execute(text(
                "SELECT 1 FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'action_execution' LIMIT 1"
            )).first())
            if not table_exists:
                findings.append(Finding(ERROR, "action_table_missing", "action_execution table is missing"))
                return result

            columns = {
                str(row[0]) for row in conn.execute(text(
                    "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'action_execution'"
                ))
            }
            missing = sorted(REQUIRED_COLUMNS - columns)
            if missing:
                findings.append(Finding(ERROR, "action_columns_missing", f"action_execution missing columns: {missing}"))
                return result

            stale_limit = _env_int("MONITOR_ACTION_STALE_LEASE_MAX_AGE_SECONDS", 300, findings, minimum=1)
            replay_limit = _env_int("MONITOR_ACTION_REPLAY_BACKLOG_MAX_AGE_SECONDS", 600, findings, minimum=1)
            missing_limit = _env_int("MONITOR_ACTION_MISSING_REFERENCE_THRESHOLD", 0, findings)
            replay_count_limit = _env_int("MONITOR_ACTION_REPLAY_BACKLOG_THRESHOLD", 0, findings)
            stale_count = int(conn.execute(text(
                "SELECT COUNT(*) FROM action_execution "
                "WHERE status='started' AND lease_until IS NOT NULL AND lease_until < UTC_TIMESTAMP(6) "
                "AND TIMESTAMPDIFF(SECOND, lease_until, UTC_TIMESTAMP(6)) > :limit"
            ), {"limit": stale_limit}).scalar() or 0)
            missing_refs = int(conn.execute(text(
                "SELECT COUNT(*) FROM action_execution WHERE status='succeeded' AND result_digest IS NULL"
            )).scalar() or 0)
            replay_backlog = int(conn.execute(text(
                "SELECT COUNT(*) FROM action_execution WHERE status='failed_retryable' "
                "AND TIMESTAMPDIFF(SECOND, created_at, UTC_TIMESTAMP(6)) > :limit"
            ), {"limit": replay_limit}).scalar() or 0)
            result.update({
                "stale_lease_count": stale_count,
                "missing_reference_count": missing_refs,
                "replay_backlog_count": replay_backlog,
            })
            if stale_count:
                findings.append(Finding(ERROR, "stale_action_leases", f"{stale_count} Action lease(s) exceed {stale_limit}s"))
            if missing_refs > missing_limit:
                findings.append(Finding(ERROR, "missing_action_references", f"{missing_refs} succeeded Action row(s) lack result_digest"))
            if replay_backlog > replay_count_limit:
                findings.append(Finding(ERROR, "action_replay_backlog", f"{replay_backlog} retryable Action row(s) exceed {replay_limit}s"))
            if not any(f.level == ERROR for f in findings):
                findings.append(Finding(INFO, "action_observation_healthy", "Action observation stop conditions are clear"))
    except Exception as exc:
        findings.append(Finding(ERROR, "database_unreachable", f"database preflight failed: {exc.__class__.__name__}: {exc}"))
    finally:
        engine.dispose()
    return result


def run(*, dsn: str | None, json_output: bool = False) -> int:
    findings: list[Finding] = []
    runtime = check_runtime_config(findings)
    database = {"skipped": True}
    if dsn:
        database = check_database(dsn, findings)
    else:
        findings.append(Finding(WARNING, "database_skipped", "no DSN supplied; runtime-only preflight"))

    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "runtime": runtime,
        "database": database,
        "findings": [asdict(item) for item in findings],
        "passed": not any(item.level == ERROR for item in findings),
    }
    if json_output:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print("action-execution C1 preflight: " + ("PASS" if report["passed"] else "FAIL"))
        for item in findings:
            print(f"[{item.level}] {item.code}: {item.message}")
    return 0 if report["passed"] else 2


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Action execution rollout gate")
    parser.add_argument("--dsn-env", default="DB_URL", help="environment variable containing SQLAlchemy DSN")
    parser.add_argument("--json", action="store_true", dest="json_output", help="emit machine-readable JSON")
    args = parser.parse_args()
    dsn = os.environ.get(args.dsn_env)
    raise SystemExit(run(dsn=dsn, json_output=args.json_output))


if __name__ == "__main__":
    main()
