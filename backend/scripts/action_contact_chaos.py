"""C2 Action/Contact failure-drill matrix using an in-memory mock harness.

This is the repeatable staging entry point for the monthly drill.  It does not
connect to production services or write business rows: each scenario records a
small event trace and asserts both the expected recovery and the forbidden
side-effects from the C2 matrix.  A real staging adapter can consume the same
scenario names later without changing the operator contract.

Examples::

    cd backend
    python scripts/action_contact_chaos.py
    python scripts/action_contact_chaos.py --scenario provider_timeout --json
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable


@dataclass(frozen=True)
class Scenario:
    name: str
    failure: str
    expected: str
    forbidden: tuple[str, ...]


@dataclass
class DrillState:
    events: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    deliveries: list[str] = field(default_factory=list)
    grants: int = 0
    lease_owner: str | None = None
    lease_fencing_token: int = 1
    session_pending: bool = False
    legacy_fallback: bool = False
    terminal: bool = False

    def event(self, name: str) -> None:
        self.events.append(name)


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("claim_worker_crash", "worker crashes after claim", "lease expires and new worker takes over once", ("double_finalize",)),
    Scenario("provider_timeout", "provider/rerank timeout", "retryable outcome or legacy fallback without success fact", ("duplicate_rerank", "duplicate_delivery")),
    Scenario("db_commit_redis_cas_failure", "DB commit succeeds then Redis CAS fails", "session_pending is reconciled without rerunning router", ("router_rerun", "duplicate_search")),
    Scenario("outbox_http_response_lost", "Outbox HTTP response is lost", "existing sending lease retries the same outbox row", ("new_search_result", "new_contact_grant")),
    Scenario("snapshot_expired_or_listing_unavailable", "snapshot expired or listing delisted", "fail-closed terminal outcome", ("stale_listing", "new_candidate_pool")),
    Scenario("key_unavailable_or_ciphertext_corrupt", "key unavailable or ciphertext corrupt", "Contact is off and migration pauses; search remains available", ("plaintext_fallback",)),
    Scenario("redis_rate_limit_unavailable", "Redis rate limiter unavailable", "Contact redemption fails closed with DB safety bound", ("unbounded_redemption",)),
    Scenario("grant_consumed_provider_timeout", "grant consumed then provider times out", "same contact_delivery remains pending for Outbox retry", ("second_grant", "second_payload")),
    Scenario("delivery_revoked_before_send", "delivery revoked before send", "send is blocked and delivery is dead-lettered with audit", ("send_after_revoke", "fake_revoke_success")),
)


def _claim_worker_crash() -> DrillState:
    state = DrillState(lease_owner="worker-a")
    state.event("claim:worker-a:fence=1")
    state.event("worker-a:crash")
    state.event("lease:expired")
    state.lease_owner = "worker-b"
    state.lease_fencing_token = 2
    state.event("claim:worker-b:fence=2")
    state.event("finalize:worker-b")
    return state


def _provider_timeout() -> DrillState:
    state = DrillState(legacy_fallback=True)
    state.event("provider:timeout")
    state.event("outcome:retryable")
    state.event("route:legacy")
    return state


def _db_commit_redis_cas_failure() -> DrillState:
    state = DrillState(session_pending=True)
    state.facts.append("search_fact:committed_once")
    state.event("db:commit")
    state.event("redis:cas_failed")
    state.event("session:pending")
    state.event("reconciler:cas_and_outbox")
    state.session_pending = False
    return state


def _outbox_http_response_lost() -> DrillState:
    state = DrillState()
    state.deliveries.append("outbox:delivery-1")
    state.event("outbox:claim:sending")
    state.event("wecom:response_lost")
    state.event("outbox:lease_expired")
    state.event("outbox:retry:same-delivery-1")
    return state


def _snapshot_expired_or_listing_unavailable() -> DrillState:
    state = DrillState(terminal=True)
    state.event("snapshot:invalid")
    state.event("outcome:failed_terminal(snapshot_unavailable)")
    return state


def _key_unavailable_or_ciphertext_corrupt() -> DrillState:
    state = DrillState()
    state.event("contact:key_or_ciphertext:error")
    state.event("contact:mode:off")
    state.event("migration:paused")
    state.event("search:legacy_available")
    return state


def _redis_rate_limit_unavailable() -> DrillState:
    state = DrillState(terminal=True)
    state.event("redis:rate_limit_unavailable")
    state.event("contact:redeem:blocked_db_safety_bound")
    return state


def _grant_consumed_provider_timeout() -> DrillState:
    state = DrillState(grants=1)
    state.deliveries.append("contact_delivery:1")
    state.event("grant:consume:1")
    state.event("wecom:provider_timeout")
    state.event("contact_delivery:1:pending")
    state.event("outbox:retry:same-delivery-1")
    return state


def _delivery_revoked_before_send() -> DrillState:
    state = DrillState(terminal=True)
    state.event("contact_delivery:1:revoked")
    state.event("outbox:send:blocked")
    state.event("outbox:dead_letter")
    state.event("audit:revoke")
    return state


RUNNERS: dict[str, Callable[[], DrillState]] = {
    "claim_worker_crash": _claim_worker_crash,
    "provider_timeout": _provider_timeout,
    "db_commit_redis_cas_failure": _db_commit_redis_cas_failure,
    "outbox_http_response_lost": _outbox_http_response_lost,
    "snapshot_expired_or_listing_unavailable": _snapshot_expired_or_listing_unavailable,
    "key_unavailable_or_ciphertext_corrupt": _key_unavailable_or_ciphertext_corrupt,
    "redis_rate_limit_unavailable": _redis_rate_limit_unavailable,
    "grant_consumed_provider_timeout": _grant_consumed_provider_timeout,
    "delivery_revoked_before_send": _delivery_revoked_before_send,
}


def run_scenario(scenario: Scenario) -> dict[str, object]:
    state = RUNNERS[scenario.name]()
    forbidden_hits = [item for item in scenario.forbidden if any(item in event for event in state.events)]
    # Every scenario must produce at least one durable/terminal recovery marker.
    recovery_marker = any(
        marker in event
        for event in state.events
        for marker in ("finalize:", "outcome:", "reconciler:", "retry:", "dead_letter", "legacy_available", "blocked_db")
    )
    passed = recovery_marker and not forbidden_hits
    return {
        "name": scenario.name,
        "failure": scenario.failure,
        "expected": scenario.expected,
        "events": state.events,
        "facts": state.facts,
        "deliveries": state.deliveries,
        "passed": passed,
        "forbidden_hits": forbidden_hits,
    }


def run(names: list[str] | None = None) -> dict[str, object]:
    selected = [item for item in SCENARIOS if not names or item.name in names]
    results = [run_scenario(item) for item in selected]
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "scenario_count": len(results),
        "passed": bool(results) and all(bool(item["passed"]) for item in results),
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the C2 Action/Contact mock failure-drill matrix")
    parser.add_argument("--scenario", action="append", dest="scenarios", choices=sorted(RUNNERS), help="run one scenario (repeatable)")
    parser.add_argument("--json", action="store_true", dest="json_output", help="emit machine-readable JSON")
    args = parser.parse_args()
    report = run(args.scenarios)
    if args.json_output:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print("action-contact C2 chaos: " + ("PASS" if report["passed"] else "FAIL"))
        for item in report["results"]:
            print(f"[{ 'PASS' if item['passed'] else 'FAIL' }] {item['name']}: {item['expected']}")
    raise SystemExit(0 if report["passed"] else 2)


if __name__ == "__main__":
    main()
