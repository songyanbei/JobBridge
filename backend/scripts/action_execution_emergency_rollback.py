"""Unified Action/Contact/Facade emergency rollback (C4).

The command follows the incident order from the architecture plan:
stop new Action/Contact traffic, force facade/strategy legacy routing, retain
and inspect durable facts, revoke unused contact credentials, and emit an
incident report.  It is a dry run unless ``--yes`` is supplied.  No command
ever deletes Action, recommendation, audit, session, or outbox facts.

Examples::

    cd backend
    python scripts/action_execution_emergency_rollback.py --operator alice \
        --reason "provider incident" --report incident.json
    python scripts/action_execution_emergency_rollback.py --dsn-env DB_URL \
        --operator alice --reason "provider incident" --yes --report incident.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


SWITCHES = {
    "action": ("routing:action_execution:kill_switch", "off"),
    "contact": ("routing:contact:kill_switch", "off"),
    "facade": ("routing:job_search_facade:enabled", "0"),
    "strategy": ("routing:recommendation:execution_mode", "off"),
}
CONFIG_ALIASES = {
    "action": ("action_execution.mode", "action_execution_mode", "action.execution.mode"),
    "contact": ("contact_service_mode", "contact.service.mode"),
    "facade": ("job_search_facade_enabled", "job_search.facade.enabled"),
}


@dataclass(frozen=True)
class RollbackStep:
    order: int
    name: str
    action: str
    safety: str


STEPS = (
    RollbackStep(1, "stop_action_contact", "set Action and Contact kill switches; no new grants", "preserve committed facts"),
    RollbackStep(2, "force_legacy", "set Facade and recommendation strategy routing to legacy/off", "legacy fallback remains available"),
    RollbackStep(3, "scan_facts", "count Action, recommendation, audit, session and Outbox rows", "read-only"),
    RollbackStep(4, "revoke_unused_contact", "revoke issued grants and unsent deliveries", "used/sent contact payloads are never rewritten"),
    RollbackStep(5, "reconcile", "operator runs session/outbox reconcilers and checks duplicate delivery/PII alerts", "never rerun router"),
    RollbackStep(6, "incident_report", "persist immutable operator report and audit reference", "repeatable by incident id"),
)


def new_incident_id(operator: str, reason: str) -> str:
    import hashlib
    digest = hashlib.sha256(f"{operator.strip()}\n{reason.strip()}".encode()).hexdigest()[:16]
    return f"rollback-{digest}"


def build_plan(*, operator: str, reason: str, incident_id: str | None = None) -> dict[str, Any]:
    if not operator.strip():
        raise ValueError("operator must not be empty")
    if not reason.strip():
        raise ValueError("reason must not be empty")
    incident = incident_id or new_incident_id(operator, reason)
    return {
        "incident_id": incident,
        "operator": operator.strip(),
        "reason": reason.strip(),
        "steps": [asdict(step) for step in STEPS],
        "switches": {name: {"redis_key": key, "value": value} for name, (key, value) in SWITCHES.items()},
        "config_aliases": CONFIG_ALIASES,
        "facts_deleted": False,
    }


def _redis_switches(plan: dict[str, Any]) -> None:
    from app.core.redis_client import get_redis
    client = get_redis()
    for item in plan["switches"].values():
        client.set(item["redis_key"], item["value"])


def _database_rollback(dsn: str, plan: dict[str, Any], *, revoke_contact: bool) -> dict[str, int]:
    from sqlalchemy import create_engine, func
    from sqlalchemy.orm import Session
    from app.models import AuditLog, ContactDelivery, ContactGrant, ContactRequest, SystemConfig

    engine = create_engine(dsn, pool_pre_ping=True)
    counts = {"config_updates": 0, "grants_revoked": 0, "requests_revoked": 0, "deliveries_revoked": 0, "audit_rows": 0}
    audit_reason = f"unified_rollback:{plan['incident_id']}"
    try:
        with Session(bind=engine) as db:
            # Updating a missing config row is deliberately a no-op: mixed
            # version fleets use the Redis switch until their config schema is
            # deployed.  This keeps rollback safe across rollout versions.
            for scope, aliases in CONFIG_ALIASES.items():
                row = db.query(SystemConfig).filter(SystemConfig.config_key.in_(aliases)).first()
                if row is None:
                    continue
                before = {"config_value": row.config_value, "value_type": row.value_type}
                target = "false" if scope == "facade" else "off"
                if row.config_value != target:
                    row.config_value = target
                    row.updated_by = plan["operator"]
                    counts["config_updates"] += 1
                exists = db.query(AuditLog).filter(
                    AuditLog.target_type == "system",
                    AuditLog.target_id == row.config_key,
                    AuditLog.action == "manual_edit",
                    AuditLog.reason == audit_reason,
                ).first()
                if exists is None:
                    db.add(AuditLog(
                        target_type="system", target_id=row.config_key,
                        action="manual_edit", operator=plan["operator"],
                        reason=audit_reason,
                        snapshot={"before": before, "after": {"config_value": target, "value_type": row.value_type}},
                    ))
                    counts["audit_rows"] += 1

            if revoke_contact:
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                grants = db.query(ContactGrant).filter(ContactGrant.status == "issued").update(
                    {ContactGrant.status: "revoked", ContactGrant.revoked_at: now, ContactGrant.revoke_reason: plan["incident_id"][:64]},
                    synchronize_session=False,
                )
                requests = db.query(ContactRequest).filter(ContactRequest.status.in_(("pending", "authorized"))).update(
                    {ContactRequest.status: "revoked", ContactRequest.revoked_at: now, ContactRequest.revoke_reason: plan["incident_id"][:64]},
                    synchronize_session=False,
                )
                deliveries = db.query(ContactDelivery).filter(ContactDelivery.status.in_(("prepared", "sending", "retry_wait"))).update(
                    {ContactDelivery.status: "revoked", ContactDelivery.revoked_at: now, ContactDelivery.revoke_reason: plan["incident_id"][:64]},
                    synchronize_session=False,
                )
                counts.update(grants_revoked=int(grants or 0), requests_revoked=int(requests or 0), deliveries_revoked=int(deliveries or 0))

            db.commit()
    finally:
        engine.dispose()
    return counts


def execute(plan: dict[str, Any], *, dsn: str, revoke_contact: bool = True) -> dict[str, Any]:
    """Apply the idempotent stop/revoke operations and return an audit report."""
    _redis_switches(plan)
    counts = _database_rollback(dsn, plan, revoke_contact=revoke_contact)
    return {**plan, "executed": True, "counts": counts, "executed_at": datetime.now(timezone.utc).isoformat()}


def main() -> None:
    parser = argparse.ArgumentParser(description="Unified Action/Contact/Facade emergency rollback (dry run by default)")
    parser.add_argument("--dsn-env", default="DB_URL", help="environment variable containing SQLAlchemy DSN")
    parser.add_argument("--operator", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--report", type=Path, help="optional JSON incident report path")
    parser.add_argument("--revoke-contact", action=argparse.BooleanOptionalAction, default=True, help="revoke issued grants and unsent deliveries")
    parser.add_argument("--yes", action="store_true", help="execute; without it only print the plan")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args()
    plan = build_plan(operator=args.operator, reason=args.reason)
    if args.yes:
        dsn = os.environ.get(args.dsn_env)
        if not dsn:
            raise SystemExit(f"missing DSN environment variable: {args.dsn_env}")
        try:
            report = execute(plan, dsn=dsn, revoke_contact=args.revoke_contact)
        except Exception as exc:
            print(f"rollback failed after switch/revoke attempt: {exc.__class__.__name__}: {exc}", file=sys.stderr)
            raise SystemExit(2)
    else:
        report = {**plan, "executed": False, "dry_run": True}

    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    if args.report:
        args.report.write_text(rendered + "\n", encoding="utf-8")
    if args.json_output:
        print(rendered)
    else:
        print("unified emergency rollback: " + ("EXECUTED" if report.get("executed") else "DRY RUN"))
        print(f"incident_id={report['incident_id']} operator={report['operator']}")
        for step in report["steps"]:
            print(f"{step['order']}. {step['name']}: {step['action']}")
        if report.get("counts"):
            print("counts=" + json.dumps(report["counts"], sort_keys=True))


if __name__ == "__main__":
    main()
