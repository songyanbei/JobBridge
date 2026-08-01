"""Deterministic recommendation-v1 scoring primitives.

All functions in this module are side-effect free.  They deliberately accept
plain dictionaries so the service can be used by SQL search, simulation and
tests without importing ORM models.
"""
from __future__ import annotations

import hashlib
import math
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from app.schemas.recommendation import Direction

V1_MAX_CANDIDATES = 50
V1_PRECISION_POOL_SIZE = 20
V1_DISPLAY_TOP_N = 3
V1_ALGORITHM_VERSION = "recommendation-v1"
MIN_EXPLORATION_MATCH_RATIO = 0.85


def clamp(value: Any, low: float = 0.0, high: float = 1.0) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return min(max(value, low), high)


def _norm_string(value: Any) -> str | None:
    if value is None:
        return None
    value = unicodedata.normalize("NFKC", str(value)).strip()
    value = " ".join(value.split()).lower()
    return value or None


def _value(candidate: Mapping[str, Any] | Any, name: str, default: Any = None) -> Any:
    if isinstance(candidate, Mapping):
        return candidate.get(name, default)
    return getattr(candidate, name, default)


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def stable_hash_hex(*parts: Any) -> str:
    return hashlib.sha256(
        "|".join("" if part is None else str(part) for part in parts).encode("utf-8")
    ).hexdigest()


def stable_bucket(*parts: Any, modulus: int = 10000) -> int:
    raw = hashlib.sha256(
        "|".join("" if part is None else str(part) for part in parts).encode("utf-8")
    ).digest()
    return int.from_bytes(raw[:8], "big") % modulus


def bucket_hit(percentage: int, *parts: Any) -> bool:
    percentage = max(0, min(100, int(percentage)))
    return stable_bucket(*parts) < percentage * 100


def _coalesce(value: float | None, default: float) -> float:
    """`clamp(...) or default` silently rewrites a legitimate 0.0 into the
    default, which shifts precision-pool membership and ranking.  Only a真正的
    None may fall back."""
    return default if value is None else value


def normalize_components_detailed(
    components: Mapping[str, tuple[float | None, float]],
    *,
    default: float = 0.5,
) -> tuple[float, int]:
    """Return (score, present_component_count).

    Weights are renormalized over the present components only (§6.2.1/§6.3.5);
    a missing component must never be silently treated as 0.
    """
    present = [(score, weight) for score, weight in components.values() if score is not None]
    if not present:
        return default, 0
    numerator = sum(float(score) * weight for score, weight in present)
    denominator = sum(weight for _, weight in present)
    if denominator <= 0:
        return default, 0
    return _coalesce(clamp(numerator / denominator), default), len(present)


def normalize_components(
    components: Mapping[str, tuple[float | None, float]],
    *,
    default: float = 0.5,
) -> float:
    return normalize_components_detailed(components, default=default)[0]


def semantic_scores(
    input_ids: Iterable[str | int],
    ranked_items: Iterable[Mapping[str, Any]] | None,
    *,
    failed: bool = False,
) -> dict[str, float]:
    ids = [str(item) for item in input_ids]
    scores = {item: 0.5 for item in ids}
    if failed or ranked_items is None:
        return scores
    seen: set[str] = set()
    for item in ranked_items:
        if not isinstance(item, Mapping) or "id" not in item:
            continue
        item_id = str(item["id"])
        if item_id not in scores or item_id in seen:
            continue
        seen.add(item_id)
        rank = len(seen)
        if rank > 3:
            break
        scores[item_id] = (1.0, 0.85, 0.70)[rank - 1]
    return scores


def extract_recommendation_v1_soft_preferences(
    criteria: Mapping[str, Any],
    direction: Direction,
) -> dict[str, Any]:
    if direction == "search_worker":
        # Candidate-search currently has no structurally comparable worker
        # preference fields; never compare job-only fields to resumes.
        return {}
    fields = (
        "provide_meal", "provide_housing", "shift_pattern",
        "accept_couple", "accept_student", "accept_minority",
        "pay_type", "district",
    )
    return {
        field: criteria[field]
        for field in fields
        if field in criteria and _has_value(criteria[field])
    }


_SOFT_WEIGHTS = {
    "provide_meal": 0.30,
    "provide_housing": 0.30,
    "shift_pattern": 0.20,
    "accept_couple": 0.10,
    "accept_student": 0.10,
    "accept_minority": 0.10,
    "pay_type": 0.10,
    "district": 0.10,
}


def soft_preference_score(
    preferences: Mapping[str, Any],
    candidate: Mapping[str, Any] | Any,
) -> float | None:
    weighted_hits = 0.0
    weighted_total = 0.0
    for field, requested in preferences.items():
        candidate_value = _value(candidate, field)
        if candidate_value is None:
            continue
        weight = _SOFT_WEIGHTS.get(field)
        if weight is None:
            continue
        weighted_total += weight
        if field in {"shift_pattern", "pay_type", "district"}:
            hit = _norm_string(requested) == _norm_string(candidate_value)
        else:
            hit = requested == candidate_value
        weighted_hits += weight if hit else 0.0
    if weighted_total == 0:
        return None
    return clamp(weighted_hits / weighted_total)


def salary_fit_score_detailed(
    direction: Direction,
    criteria: Mapping[str, Any],
    candidate: Mapping[str, Any] | Any,
) -> tuple[float | None, bool]:
    """Return (score, hard_filter_contract_broken).

    §6.3.3: a candidate whose pay cannot satisfy the query should already have
    been removed by the hard filter.  If one still arrives here the salary score
    is 0 *and* the候选 must be dropped from the result, not merely down-ranked.
    """
    if direction == "search_job":
        query_salary = criteria.get("salary_floor_monthly")
        if query_salary is None:
            return None, False
        effective_max = _value(candidate, "salary_ceiling_monthly")
        if effective_max is None:
            effective_max = _value(candidate, "salary_floor_monthly")
        try:
            q = float(query_salary)
            effective_max = float(effective_max)
        except (TypeError, ValueError):
            return None, False
        if not math.isfinite(q) or not math.isfinite(effective_max):
            return None, False
        if effective_max < q:
            return 0.0, True
        return 0.5 + 0.5 * min(max((effective_max - q) / max(0.3 * q, 1), 0), 1), False

    budget = criteria.get("salary_ceiling_monthly")
    expectation = _value(candidate, "salary_expect_floor_monthly")
    if budget is None or expectation is None:
        return None, False
    try:
        budget = float(budget)
        expectation = float(expectation)
    except (TypeError, ValueError):
        return None, False
    if not math.isfinite(budget) or not math.isfinite(expectation):
        return None, False
    if expectation > budget:
        return 0.0, True
    return 0.5 + 0.5 * min(max((budget - expectation) / max(0.3 * budget, 1), 0), 1), False


def salary_fit_score(
    direction: Direction,
    criteria: Mapping[str, Any],
    candidate: Mapping[str, Any] | Any,
) -> float | None:
    return salary_fit_score_detailed(direction, criteria, candidate)[0]


def quality_score(candidate: Mapping[str, Any] | Any, direction: Direction) -> float:
    fields = (
        (
            "salary_ceiling_monthly", "district", "description", "provide_meal",
            "provide_housing", "shift_pattern", "work_hours", "employment_type",
        )
        if direction == "search_job"
        else (
            "description", "education", "work_experience", "expected_districts",
            "available_from", "accept_night_shift", "accept_overtime",
            "accept_long_term", "accept_short_term",
        )
    )
    return sum(_has_value(_value(candidate, field)) for field in fields) / len(fields)


FRESHNESS_HALF_LIFE_HOURS = 72


def freshness_score_detailed(created_at: Any, now: datetime | None = None) -> tuple[float, str | None]:
    """Return (score, diagnostic_reason).

    §6.5: missing/illegal `created_at` scores 0.5 and records a reason; a future
    timestamp is treated as age 0 and records a clock anomaly.
    """
    now = now or datetime.now(timezone.utc)
    try:
        created = created_at
        if isinstance(created, str):
            created = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        delta_hours = (
            now.astimezone(timezone.utc) - created.astimezone(timezone.utc)
        ).total_seconds() / 3600
    except (AttributeError, TypeError, ValueError):
        return 0.5, "freshness_created_at_invalid"
    reason = "freshness_clock_anomaly" if delta_hours < 0 else None
    age_hours = max(delta_hours, 0)
    return 2 ** (-age_hours / FRESHNESS_HALF_LIFE_HOURS), reason


def freshness_score(created_at: Any, now: datetime | None = None) -> float:
    return freshness_score_detailed(created_at, now)[0]


def match_score_detailed(
    *,
    semantic: float | None,
    salary: float | None,
    soft: float | None,
) -> tuple[float, bool]:
    """Return (match_score, all_components_missing) per §6.3.5."""
    value, present = normalize_components_detailed(
        {"semantic": (clamp(semantic), 0.50), "salary": (clamp(salary), 0.30), "soft": (clamp(soft), 0.20)}
    )
    return value, present == 0


def match_score(
    *,
    semantic: float | None,
    salary: float | None,
    soft: float | None,
) -> float:
    return match_score_detailed(semantic=semantic, salary=salary, soft=soft)[0]


def pre_score(
    *,
    salary: float | None,
    soft: float | None,
    quality: float | None,
) -> float:
    return normalize_components(
        {"salary": (clamp(salary), 0.60), "soft": (clamp(soft), 0.30), "quality": (clamp(quality), 0.10)}
    )


def base_score(
    *,
    match: float,
    quality: float,
    freshness: float,
    exposure: float,
    match_weight: int = 70,
    quality_weight: int = 10,
    freshness_weight: int = 8,
    exposure_weight: int = 12,
) -> float:
    total = match_weight + quality_weight + freshness_weight + exposure_weight
    if total <= 0:
        return 0.5
    return _coalesce(
        clamp(
            (
                match_weight * match
                + quality_weight * quality
                + freshness_weight * freshness
                + exposure_weight * exposure
            ) / total
        ),
        0.5,
    )


# `eq=False` keeps identity semantics.  The diversity greedy uses `candidate not
# in near` / `candidate not in selected`, and field-wise equality would both
# conflate two candidates that happen to score identically and make the dataclass
# unhashable.
@dataclass(eq=False)
class ScoredCandidate:
    candidate_id: str
    owner_userid: str | None
    data: Mapping[str, Any] = field(default_factory=dict)
    semantic_score: float = 0.5
    salary_score: float | None = None
    soft_score: float | None = None
    quality_score: float = 0.5
    freshness_score: float = 0.5
    exposure_count: int = 0
    exposure_opportunity: float = 0.5
    match_score: float = 0.5
    base_score: float = 0.5
    repeat_factor: float = 1.0
    repeat_adjusted_score: float = 0.5
    # Diagnostics surfaced into the snapshot / attempt facts.
    diversity_penalty: float = 0.0
    repeat_bucket: str = "unseen"
    layer: str = "near"
    hard_filter_contract_broken: bool = False
    match_components_all_missing: bool = False
    is_exploration: bool = False
    reason_codes: list[str] = field(default_factory=list)


def build_scored_candidate(
    candidate: Mapping[str, Any],
    *,
    candidate_id: str | int,
    owner_userid: str | None,
    direction: Direction,
    criteria: Mapping[str, Any],
    semantic: float = 0.5,
    exposure_opportunity: float = 0.5,
    exposure_count: int = 0,
    now: datetime | None = None,
) -> ScoredCandidate:
    salary, contract_broken = salary_fit_score_detailed(direction, criteria, candidate)
    preferences = extract_recommendation_v1_soft_preferences(criteria, direction)
    soft = soft_preference_score(preferences, candidate)
    quality = quality_score(candidate, direction)
    freshness, freshness_reason = freshness_score_detailed(_value(candidate, "created_at"), now)
    scored = ScoredCandidate(
        candidate_id=str(candidate_id),
        owner_userid=owner_userid,
        data=candidate,
        semantic_score=_coalesce(clamp(semantic), 0.5),
        salary_score=salary,
        soft_score=soft,
        quality_score=quality,
        freshness_score=freshness,
        exposure_count=int(exposure_count or 0),
        exposure_opportunity=_coalesce(clamp(exposure_opportunity), 0.5),
        hard_filter_contract_broken=contract_broken,
    )
    scored.match_score, scored.match_components_all_missing = match_score_detailed(
        semantic=scored.semantic_score, salary=salary, soft=soft
    )
    scored.base_score = base_score(
        match=scored.match_score,
        quality=scored.quality_score,
        freshness=scored.freshness_score,
        exposure=scored.exposure_opportunity,
    )
    scored.repeat_adjusted_score = scored.base_score
    if freshness_reason:
        scored.reason_codes.append(freshness_reason)
    if contract_broken:
        scored.reason_codes.append("hard_filter_contract_broken")
    if scored.match_components_all_missing:
        scored.reason_codes.append("match_components_all_missing")
    return scored
