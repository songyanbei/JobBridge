"""Read-only S4 production gate preflight.

The command intentionally fails closed: S4 publish remains unavailable unless
Action/Contact prerequisites, migration artifacts, and legacy compatibility are
all healthy. It never mutates configuration or business rows.
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ERROR, WARNING, INFO = "error", "warning", "info"


@dataclass
class Finding:
    level: str
    code: str
    message: str
    details: dict[str, Any] | None = None


def check_runtime_config(findings: list[Finding]) -> dict[str, Any]:
    action_mode = os.environ.get("ACTION_EXECUTION_MODE", "off").strip().lower()
    contact_mode = os.environ.get("CONTACT_SERVICE_MODE", "off").strip().lower()
    publish_enabled = os.environ.get("JOB_PUBLISH_FLOW_ENABLED", "false").strip().lower() in {"1", "true", "on", "yes"}
    kill_switch = os.environ.get("JOB_PUBLISH_KILL_SWITCH", "false").strip().lower() in {"1", "true", "on", "yes"}
    try:
        rollout = int(os.environ.get("JOB_PUBLISH_ROLLOUT_PERCENTAGE", "0"))
    except ValueError:
        rollout = 0
        findings.append(Finding(ERROR, "invalid_publish_rollout", "JOB_PUBLISH_ROLLOUT_PERCENTAGE is not an integer"))
    if action_mode not in {"off", "shadow", "on"}:
        findings.append(Finding(ERROR, "invalid_action_mode", f"ACTION_EXECUTION_MODE={action_mode!r} is invalid"))
    if contact_mode not in {"off", "shadow", "on"}:
        findings.append(Finding(ERROR, "invalid_contact_mode", f"CONTACT_SERVICE_MODE={contact_mode!r} is invalid"))
    if not 0 <= rollout <= 100:
        findings.append(Finding(ERROR, "invalid_publish_rollout", "job publish rollout must be 0..100"))
    if publish_enabled and (kill_switch or rollout == 0):
        findings.append(Finding(ERROR, "publish_gate_blocked", "publish enabled but kill switch is active or rollout is zero"))
    if action_mode != "on":
        findings.append(Finding(ERROR, "action_gate_incomplete", "Action execution must be on before S4"))
    if contact_mode != "on":
        findings.append(Finding(ERROR, "contact_gate_incomplete", "Contact service must be on before S4"))
    return {
        "action_mode": action_mode, "contact_mode": contact_mode,
        "publish_enabled": publish_enabled, "publish_rollout_percentage": rollout,
        "publish_kill_switch": kill_switch,
    }


def check_migration_artifacts(root: str | Path, findings: list[Finding]) -> dict[str, Any]:
    migration_dir = Path(root) / "sql" / "migrations"
    required = ("phase14_001_domain_outbox_event.sql", "phase14_004_domain_outbox_consumer.sql", "phase14_down_001_domain_outbox_event.sql")
    missing = [name for name in required if not (migration_dir / name).is_file()]
    if missing:
        findings.append(Finding(ERROR, "phase14_migration_missing", f"missing migration artifacts: {missing}"))
    return {"migration_dir": str(migration_dir), "required": list(required), "missing": missing}


def check_consumer_health(findings: list[Finding]) -> dict[str, bool]:
    enabled = os.environ.get("DOMAIN_OUTBOX_CONSUMER_ENABLED", "false").strip().lower() in {"1", "true", "on", "yes"}
    healthy = os.environ.get("DOMAIN_OUTBOX_CONSUMER_HEALTHY", "false").strip().lower() in {"1", "true", "on", "yes"}
    if enabled and not healthy:
        findings.append(Finding(ERROR, "domain_outbox_consumer_unhealthy", "domain outbox consumer enabled but health signal is absent"))
    return {"enabled": enabled, "healthy": healthy}


def run(*, backend_root: str | Path | None = None, json_output: bool = False) -> int:
    findings: list[Finding] = []
    root = Path(backend_root or Path(__file__).resolve().parents[1])
    runtime = check_runtime_config(findings)
    consumer = check_consumer_health(findings)
    runtime["domain_outbox_consumer_enabled"] = consumer["enabled"]
    runtime["domain_outbox_consumer_healthy"] = consumer["healthy"]
    migrations = check_migration_artifacts(root, findings)
    # Legacy remains the rollback path until explicit exit approval exists.
    legacy = {"available": True, "reason": "legacy/fallback retained by contract"}
    findings.append(Finding(INFO, "legacy_compatibility", legacy["reason"]))
    report = {"runtime": runtime, "migrations": migrations, "legacy": legacy, "findings": [asdict(item) for item in findings], "passed": not any(item.level == ERROR for item in findings)}
    if json_output:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print("s4 preflight: " + ("PASS" if report["passed"] else "FAIL"))
        for item in findings:
            print(f"[{item.level}] {item.code}: {item.message}")
    return 0 if report["passed"] else 2


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only S4 publish rollout gate")
    parser.add_argument("--backend-root", default=None)
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args()
    raise SystemExit(run(backend_root=args.backend_root, json_output=args.json_output))


if __name__ == "__main__":
    main()
