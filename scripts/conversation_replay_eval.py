"""Replay anonymized historical text turns against the configured intent pipeline.

This tool is read-only: it never routes messages or writes session/business data. Raw
conversation text is not emitted; reports contain only hashes, labels, counts and latency.
Phone/ID-like numbers, URLs and email addresses are redacted before provider calls.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from app.config import settings
from app.core.logging_setup import configure_loguru
from app.db import SessionLocal
from app.models import ConversationLog, User
from app.schemas.conversation import CandidateSnapshot, SessionState
from app.services.intent_service import classify_dialogue


_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_ID_CARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\w)")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_URL = re.compile(r"https?://\S+", re.IGNORECASE)


def _redact(text: str) -> str:
    text = _PHONE.sub("[PHONE]", text or "")
    text = _ID_CARD.sub("[ID]", text)
    text = _EMAIL.sub("[EMAIL]", text)
    return _URL.sub("[URL]", text)


def _case_hash(userid: str, row_id: int) -> str:
    return hashlib.sha256(f"{userid}:{row_id}".encode()).hexdigest()[:12]


def _intent_family(intent: str | None, prior_direction: str | None) -> str | None:
    if intent == "search_job" or (intent == "follow_up" and prior_direction == "search_job"):
        return "job_search"
    if intent == "search_worker" or (
        intent == "follow_up" and prior_direction == "search_worker"
    ):
        return "candidate_search"
    if intent == "upload_job":
        return "job_upload"
    if intent == "upload_resume":
        return "resume_upload"
    return intent


@dataclass
class ReplayCase:
    case_id: str
    role: str
    text: str
    expected_intent: str
    history: list[dict] = field(default_factory=list)
    criteria: dict = field(default_factory=dict)
    prior_direction: str | None = None


def _unreplayable_reason(case: ReplayCase) -> str | None:
    """Return why a historical label cannot be fairly replayed from stored fields."""
    # Pagination requires candidate_snapshot, which conversation_log does not persist.
    if case.expected_intent == "show_more":
        return "missing_candidate_snapshot"
    # follow_up without prior criteria may belong to an upload draft; draft state is absent.
    if case.expected_intent == "follow_up" and not case.criteria:
        return "missing_pending_flow"
    # User.role is mutable and logs do not version it. These pairs prove role drift, not parser
    # error, so exclude them from compatibility agreement and report their count separately.
    if case.role == "worker" and case.expected_intent in {"search_worker", "upload_job"}:
        return "role_label_conflict"
    if case.role == "factory" and case.expected_intent in {"search_job", "upload_resume"}:
        return "role_label_conflict"
    return None


def load_cases() -> list[ReplayCase]:
    """Pair each inbound text row with the first labelled outbound before next input."""
    db = SessionLocal()
    try:
        rows = (
            db.query(ConversationLog, User.role)
            .join(User, User.external_userid == ConversationLog.userid)
            .filter(ConversationLog.msg_type == "text")
            .order_by(ConversationLog.userid, ConversationLog.id)
            .all()
        )
    finally:
        db.close()

    by_user: dict[str, list[tuple[ConversationLog, str]]] = defaultdict(list)
    for log, role in rows:
        by_user[log.userid].append((log, role))

    cases: list[ReplayCase] = []
    for userid, user_rows in by_user.items():
        history: list[dict] = []
        last_criteria: dict = {}
        last_direction: str | None = None
        pending: tuple[ConversationLog, str, list[dict], dict, str | None] | None = None
        for log, role in user_rows:
            if log.direction == "in":
                pending = (
                    log, role, list(history[-12:]), dict(last_criteria), last_direction,
                )
                history.append({"role": "user", "content": _redact(log.content)})
                continue
            if log.criteria_snapshot:
                snapshot = dict(log.criteria_snapshot)
                wrapped = snapshot.get("criteria")
                last_criteria = dict(wrapped) if isinstance(wrapped, dict) else snapshot
                snapshot_direction = snapshot.get("broker_direction")
                if snapshot_direction in {"search_job", "search_worker"}:
                    last_direction = snapshot_direction
            if log.intent == "search_job":
                last_direction = "search_job"
            elif log.intent == "search_worker":
                last_direction = "search_worker"
            if pending is None or not log.intent:
                continue
            inbound, pending_role, prior_history, prior_criteria, prior_direction = pending
            cases.append(ReplayCase(
                case_id=_case_hash(userid, int(inbound.id)),
                role=pending_role,
                text=_redact(inbound.content),
                expected_intent=log.intent,
                history=prior_history,
                criteria=prior_criteria,
                prior_direction=prior_direction,
            ))
            pending = None
            history.append({"role": "assistant", "content": _redact(log.content)})
    return cases


def _stratified(cases: list[ReplayCase], limit: int) -> list[ReplayCase]:
    ordered = sorted(cases, key=lambda c: (c.role, c.expected_intent, c.case_id))
    if limit <= 0 or len(ordered) <= limit:
        return ordered
    buckets: dict[tuple[str, str], list[ReplayCase]] = defaultdict(list)
    for case in ordered:
        buckets[(case.role, case.expected_intent)].append(case)
    picked: list[ReplayCase] = []
    while len(picked) < limit and buckets:
        for key in list(sorted(buckets)):
            if len(picked) >= limit:
                break
            picked.append(buckets[key].pop(0))
            if not buckets[key]:
                del buckets[key]
    return picked


def run(
    limit: int,
    repeat: int,
    cases_override: list[ReplayCase] | None = None,
    source_label: str | None = None,
) -> dict:
    all_cases = cases_override if cases_override is not None else load_cases()
    excluded = Counter(
        reason for case in all_cases
        if (reason := _unreplayable_reason(case)) is not None
    )
    replayable = [case for case in all_cases if _unreplayable_reason(case) is None]
    cases = _stratified(replayable, limit)
    original_mode = settings.dialogue_v2_mode
    original_policy = settings.dialogue_policy
    settings.dialogue_v2_mode = "primary"
    settings.dialogue_policy = settings.dialogue_policy.model_copy(
        update={"primary_rollout_percentage": 100},
    )
    outcomes: list[dict] = []
    try:
        for run_index in range(repeat):
            for case in cases:
                history = list(case.history)
                # message_router records the current user turn before classification.
                history.append({"role": "user", "content": case.text})
                has_search_context = bool(case.criteria and case.prior_direction)
                session = SessionState(
                    role=case.role,
                    search_criteria=dict(case.criteria),
                    history=history,
                    broker_direction=case.prior_direction if case.role == "broker" else None,
                    candidate_snapshot=(
                        CandidateSnapshot(
                            candidate_ids=["eval-placeholder"],
                            effective_criteria=dict(case.criteria),
                        ) if has_search_context else None
                    ),
                    active_flow="search_active" if has_search_context else "idle",
                )
                started = time.perf_counter()
                error = None
                actual = None
                source = None
                try:
                    result = classify_dialogue(
                        case.text,
                        case.role,
                        history=history,
                        session=session,
                        user_msg_id=f"eval-{case.case_id}-{run_index}",
                        userid=f"eval-{case.case_id}",
                    )
                    actual = result.intent_result.intent
                    source = result.source
                    resolved_frame = getattr(
                        getattr(result, "decision", None),
                        "resolved_frame",
                        None,
                    )
                except Exception as exc:  # report provider/degradation errors, never raw text
                    error = type(exc).__name__
                    resolved_frame = None
                expected_family = _intent_family(
                    case.expected_intent, case.prior_direction,
                )
                actual_family = (
                    resolved_frame if resolved_frame not in {None, "none"}
                    else _intent_family(actual, case.prior_direction)
                )
                outcomes.append({
                    "case_id": case.case_id,
                    "run": run_index,
                    "role": case.role,
                    "expected": case.expected_intent,
                    "actual": actual,
                    "expected_family": expected_family,
                    "actual_family": actual_family,
                    "source": source,
                    "error": error,
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                })
    finally:
        settings.dialogue_v2_mode = original_mode
        settings.dialogue_policy = original_policy

    latencies = sorted(row["latency_ms"] for row in outcomes)
    correct = sum(row["actual"] == row["expected"] for row in outcomes)
    semantic_correct = sum(
        row["actual_family"] == row["expected_family"] for row in outcomes
    )
    consistent = 0
    by_case: dict[str, set[str | None]] = defaultdict(set)
    for row in outcomes:
        by_case[row["case_id"]].add(row["actual"])
    consistent = sum(len(values) == 1 for values in by_case.values())
    return {
        "source": source_label or (
            "curated" if cases_override is not None else "historical"
        ),
        "historical_case_count": len(all_cases),
        "replayable_case_count": len(replayable),
        "excluded_case_count_by_reason": dict(excluded),
        "evaluated_case_count": len(cases),
        "repeat": repeat,
        "role_coverage": dict(Counter(c.role for c in cases)),
        "expected_intent_coverage": dict(Counter(c.expected_intent for c in cases)),
        "legacy_compatibility_agreement": (
            round(correct / len(outcomes), 4) if outcomes else None
        ),
        "semantic_family_agreement": (
            round(semantic_correct / len(outcomes), 4) if outcomes else None
        ),
        "stable_case_rate": round(consistent / len(by_case), 4) if by_case else None,
        "error_count": sum(bool(row["error"]) for row in outcomes),
        "fallback_count": sum("fallback" in (row["source"] or "") for row in outcomes),
        "latency_ms": {
            "mean": round(statistics.mean(latencies), 1) if latencies else None,
            "p95": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
            if latencies else None,
            "max": max(latencies) if latencies else None,
        },
        "semantic_mismatches": [
            row for row in outcomes
            if row["actual_family"] != row["expected_family"]
        ],
        "exact_mismatches": [
            row for row in outcomes
            if row["actual"] != row["expected"]
        ],
    }


def main() -> None:
    configure_loguru(settings.app_env)
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--output", help="optional JSON report path")
    parser.add_argument(
        "--allow-errors", action="store_true",
        help="emit a report with provider/runtime errors instead of exiting non-zero",
    )
    parser.add_argument("--min-semantic-agreement", type=float)
    parser.add_argument("--max-fallback-rate", type=float)
    parser.add_argument(
        "--curated", action="store_true",
        help="use the manually labelled, PII-free role/intent matrix",
    )
    parser.add_argument(
        "--synthetic-matrix", action="store_true",
        help="use the deterministic 600-case PII-free breadth matrix",
    )
    parser.add_argument(
        "--case-id", action="append", default=[],
        help="evaluate only selected curated case id; may be repeated",
    )
    args = parser.parse_args()
    cases = None
    source_label = "historical"
    if args.curated and args.synthetic_matrix:
        parser.error("--curated and --synthetic-matrix are mutually exclusive")
    if args.curated:
        from app.evaluation.curated_conversation_cases import CURATED_CASES
        cases = [ReplayCase(**case) for case in CURATED_CASES]
        if args.case_id:
            selected = set(args.case_id)
            cases = [case for case in cases if case.case_id in selected]
        source_label = "manually_curated"
    elif args.synthetic_matrix:
        from app.evaluation.synthetic_intent_matrix import SYNTHETIC_INTENT_MATRIX
        cases = [ReplayCase(**case) for case in SYNTHETIC_INTENT_MATRIX]
        if args.case_id:
            selected = set(args.case_id)
            cases = [case for case in cases if case.case_id in selected]
        source_label = "synthetic_matrix"
    report = run(
        max(0, args.limit),
        max(1, args.repeat),
        cases_override=cases,
        source_label=source_label,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(rendered + "\n")
    print(rendered)
    evaluated_calls = report["evaluated_case_count"] * report["repeat"]
    if report["error_count"] and not args.allow_errors:
        raise SystemExit(1)
    if (
        args.min_semantic_agreement is not None
        and (
            report["semantic_family_agreement"] is None
            or report["semantic_family_agreement"] < args.min_semantic_agreement
        )
    ):
        raise SystemExit(1)
    fallback_rate = (
        report["fallback_count"] / evaluated_calls if evaluated_calls else 0.0
    )
    if (
        args.max_fallback_rate is not None
        and fallback_rate > args.max_fallback_rate
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
