"""Deterministic MatchingPolicy v1 for bidirectional recruitment search."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

POLICY_VERSION = "matching-policy-v1"
DIRECTIONS = ("worker_to_job", "factory_to_worker", "broker_to_job", "broker_to_worker")


def direction_for(role: str, search_direction: str) -> str:
    mapping = {
        ("worker", "search_job"): "worker_to_job",
        ("factory", "search_worker"): "factory_to_worker",
        ("broker", "search_job"): "broker_to_job",
        ("broker", "search_worker"): "broker_to_worker",
    }
    try:
        return mapping[(str(role), str(search_direction))]
    except KeyError as exc:
        raise ValueError("unsupported_recruitment_direction") from exc


def policy_digest(policy_version: str = POLICY_VERSION) -> str:
    return hashlib.sha256(str(policy_version).encode("utf-8")).hexdigest()


def _now(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value.replace(tzinfo=None) if value.tzinfo else value


def hard_filter(candidate: Mapping[str, Any], *, now: datetime | None = None) -> bool:
    """Apply the server-owned visibility predicate before any ranking."""
    moment = _now(now)
    if candidate.get("audit_status") != "passed":
        return False
    if candidate.get("deleted_at") is not None or candidate.get("delist_reason") is not None:
        return False
    expires_at = candidate.get("expires_at")
    if expires_at is None:
        return False
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            return False
    return _now(expires_at) > moment


@dataclass(frozen=True)
class MatchDecision:
    included: bool
    score: float
    reasons: tuple[str, ...]
    policy_version: str = POLICY_VERSION
    policy_digest: str = policy_digest()


class MatchingPolicyV1:
    """Bounded, explainable reranker. It never sees contact PII."""

    version = POLICY_VERSION
    digest = policy_digest()
    max_candidates = 100

    def evaluate(self, candidate: Mapping[str, Any], criteria: Mapping[str, Any] | None = None, *, now: datetime | None = None) -> MatchDecision:
        if not hard_filter(candidate, now=now):
            return MatchDecision(False, 0.0, ("hard_filter_excluded",))
        criteria = criteria or {}
        score = 0.0
        reasons: list[str] = []
        wanted_cities = criteria.get("city") or criteria.get("cities") or []
        wanted_cities = [wanted_cities] if isinstance(wanted_cities, str) else list(wanted_cities)
        candidate_cities = candidate.get("expected_cities") or candidate.get("city") or []
        candidate_cities = [candidate_cities] if isinstance(candidate_cities, str) else list(candidate_cities)
        if wanted_cities and set(wanted_cities) & set(candidate_cities):
            score += 1.0
            reasons.append("city_match")
        wanted_categories = criteria.get("job_category") or criteria.get("job_categories") or []
        wanted_categories = [wanted_categories] if isinstance(wanted_categories, str) else list(wanted_categories)
        candidate_categories = candidate.get("expected_job_categories") or candidate.get("job_category") or []
        candidate_categories = [candidate_categories] if isinstance(candidate_categories, str) else list(candidate_categories)
        if wanted_categories and set(wanted_categories) & set(candidate_categories):
            score += 1.0
            reasons.append("category_match")
        salary = criteria.get("salary_floor_monthly") or criteria.get("salary_ceiling_monthly")
        candidate_salary = candidate.get("salary_expect_floor_monthly") or candidate.get("salary_floor_monthly")
        if salary is not None and candidate_salary is not None:
            if criteria.get("salary_ceiling_monthly") is not None and candidate_salary <= criteria["salary_ceiling_monthly"]:
                score += 0.5; reasons.append("salary_match")
            elif criteria.get("salary_floor_monthly") is not None and candidate_salary >= criteria["salary_floor_monthly"]:
                score += 0.5; reasons.append("salary_match")
        return MatchDecision(True, score, tuple(reasons))

    def rank(self, candidates: list[Mapping[str, Any]], criteria: Mapping[str, Any] | None = None, *, now: datetime | None = None, limit: int | None = None) -> list[Mapping[str, Any]]:
        scored = [(self.evaluate(item, criteria, now=now), item) for item in candidates[: self.max_candidates]]
        scored = [(decision, item) for decision, item in scored if decision.included]
        scored.sort(key=lambda pair: (-pair[0].score, str(pair[1].get("id", ""))))
        return [item for _, item in scored[: (limit or self.max_candidates)]]


__all__ = ["DIRECTIONS", "POLICY_VERSION", "MatchingPolicyV1", "MatchDecision", "direction_for", "hard_filter", "policy_digest"]

