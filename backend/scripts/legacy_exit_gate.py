"""C3 legacy-exit eligibility gate.

The gate only evaluates evidence.  It never changes routing, deletes legacy
code, or removes historical data.  A successful result means an RFC may be
proposed; the actual "stop new legacy traffic" change still requires a
separate approval and the C4 rollback drill.

Input JSON shape::

    {
      "action_on_coverage": [99.2, ... 14 daily values ...],
      "replay_recovery_success_rate": [99.95, ...],
      "duplicate_provider_calls": [0, ...],
      "contact_pii_leaks": [0, ...],
      "contact_token_replays": [0, ...],
      "golden_diffs_approved": true,
      "legacy_compatibility": true,
      "pending_action_count": 0,
      "pending_session_count": 0,
      "pending_outbox_count": 0,
      "rollback_drill_passed": true
    }
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Mapping, Sequence


WINDOW_DAYS = 14
MIN_ACTION_COVERAGE = 99.0
MIN_REPLAY_RECOVERY = 99.9


@dataclass(frozen=True)
class GateFinding:
    code: str
    passed: bool
    message: str
    details: dict[str, Any] | None = None


def _window(values: Any, name: str, findings: list[GateFinding]) -> list[float]:
    if not isinstance(values, (list, tuple)):
        findings.append(GateFinding(f"{name}_missing", False, f"{name} must contain {WINDOW_DAYS} daily values"))
        return []
    if len(values) < WINDOW_DAYS:
        findings.append(GateFinding(f"{name}_short_window", False, f"{name} has {len(values)} days; need {WINDOW_DAYS}"))
    parsed: list[float] = []
    for value in values:
        try:
            parsed.append(float(value))
        except (TypeError, ValueError):
            findings.append(GateFinding(f"{name}_invalid", False, f"{name} contains non-numeric value {value!r}"))
    return parsed


def _bool(data: Mapping[str, Any], name: str, findings: list[GateFinding]) -> bool:
    value = data.get(name)
    if value is not True:
        findings.append(GateFinding(name, False, f"{name} must be explicitly true"))
        return False
    findings.append(GateFinding(name, True, f"{name}=true"))
    return True


def evaluate(data: Mapping[str, Any]) -> dict[str, Any]:
    findings: list[GateFinding] = []
    coverage = _window(data.get("action_on_coverage"), "action_on_coverage", findings)
    replay = _window(data.get("replay_recovery_success_rate"), "replay_recovery_success_rate", findings)
    duplicates = _window(data.get("duplicate_provider_calls"), "duplicate_provider_calls", findings)
    pii = _window(data.get("contact_pii_leaks"), "contact_pii_leaks", findings)
    replays = _window(data.get("contact_token_replays"), "contact_token_replays", findings)

    if len(coverage) >= WINDOW_DAYS and min(coverage[-WINDOW_DAYS:]) >= MIN_ACTION_COVERAGE:
        findings.append(GateFinding("action_coverage", True, "Action on coverage is >=99% for 14 days", {"minimum": min(coverage[-WINDOW_DAYS:])}))
    else:
        findings.append(GateFinding("action_coverage", False, "Action on coverage must stay >=99% for 14 days"))
    if len(replay) >= WINDOW_DAYS and min(replay[-WINDOW_DAYS:]) >= MIN_REPLAY_RECOVERY:
        findings.append(GateFinding("replay_recovery", True, "Replay/recovery success is >=99.9% for 14 days", {"minimum": min(replay[-WINDOW_DAYS:])}))
    else:
        findings.append(GateFinding("replay_recovery", False, "Replay/recovery success must stay >=99.9% for 14 days"))
    if len(duplicates) >= WINDOW_DAYS and max(duplicates[-WINDOW_DAYS:]) == 0:
        findings.append(GateFinding("duplicate_provider_calls", True, "No duplicate provider calls for 14 days"))
    else:
        findings.append(GateFinding("duplicate_provider_calls", False, "Duplicate provider calls must remain zero for 14 days"))
    if len(pii) >= WINDOW_DAYS and max(pii[-WINDOW_DAYS:]) == 0:
        findings.append(GateFinding("contact_pii_leaks", True, "No Contact PII leaks for 14 days"))
    else:
        findings.append(GateFinding("contact_pii_leaks", False, "Contact PII leaks must remain zero for 14 days"))
    if len(replays) >= WINDOW_DAYS and max(replays[-WINDOW_DAYS:]) == 0:
        findings.append(GateFinding("contact_token_replays", True, "No Contact token replays for 14 days"))
    else:
        findings.append(GateFinding("contact_token_replays", False, "Contact token replays must remain zero for 14 days"))

    for name in ("golden_diffs_approved", "legacy_compatibility", "rollback_drill_passed"):
        _bool(data, name, findings)
    for name in ("pending_action_count", "pending_session_count", "pending_outbox_count"):
        try:
            value = int(data.get(name, -1))
        except (TypeError, ValueError):
            value = -1
        ok = value == 0
        findings.append(GateFinding(name, ok, f"{name}={value}; must be zero"))

    return {
        "eligible_to_propose_rfc": all(item.passed for item in findings),
        "window_days": WINDOW_DAYS,
        "findings": [asdict(item) for item in findings],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the C3 legacy-exit evidence gate")
    parser.add_argument("--evidence", required=True, type=Path, help="JSON evidence file; the file is read-only")
    parser.add_argument("--json", action="store_true", dest="json_output", help="emit machine-readable JSON")
    args = parser.parse_args()
    try:
        evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"cannot read evidence: {exc}")
    if not isinstance(evidence, Mapping):
        raise SystemExit("evidence root must be a JSON object")
    report = evaluate(evidence)
    if args.json_output:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print("legacy-exit C3 gate: " + ("ELIGIBLE" if report["eligible_to_propose_rfc"] else "BLOCKED"))
        for finding in report["findings"]:
            print(f"[{ 'PASS' if finding['passed'] else 'BLOCK' }] {finding['code']}: {finding['message']}")
    raise SystemExit(0 if report["eligible_to_propose_rfc"] else 2)


if __name__ == "__main__":
    main()
