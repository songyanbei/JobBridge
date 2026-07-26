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


def normalize_components(
    components: Mapping[str, tuple[float | None, float]],
    *,
    default: float = 0.5,
) -> float:
    present = [(score, weight) for score, weight in components.values() if score is not None]
    if not present:
        return default
    numerator = sum(float(score) * weight for score, weight in present)
    denominator = sum(weight for _, weight in present)
    return clamp(numerator / denominator) or default


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


def salary_fit_score(
    direction: Direction,
    criteria: Mapping[str, Any],
    candidate: Mapping[str, Any] | Any,
) -> float | None:
    if direction == "search_job":
        query_salary = criteria.get("salary_floor_monthly")
        if query_salary is None:
            return None
        effective_max = _value(candidate, "salary_ceiling_monthly")
        if effective_max is None:
            effective_max = _value(candidate, "salary_floor_monthly")
        try:
            q = float(query_salary)
            effective_max = float(effective_max)
        except (TypeError, ValueError):
            return None
        if effective_max < q:
            return 0.0
        return 0.5 + 0.5 * min(max((effective_max - q) / max(0.3 * q, 1), 0), 1)

    budget = criteria.get("salary_ceiling_monthly")
    expectation = _value(candidate, "salary_expect_floor_monthly")
    if budget is None or expectation is None:
        return None
    try:
        budget = float(budget)
        expectation = float(expectation)
    except (TypeError, ValueError):
        return None
    if expectation > budget:
        return 0.0
    return 0.5 + 0.5 * min(max((budget - expectation) / max(0.3 * budget, 1), 0), 1)


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


def freshness_score(created_at: Any, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    try:
        created = created_at
        if isinstance(created, str):
            created = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_hours = max((now.astimezone(timezone.utc) - created.astimezone(timezone.utc)).total_seconds() / 3600, 0)
    except (AttributeError, TypeError, ValueError):
        return 0.5
    return 2 ** (-age_hours / 72)


def match_score(
    *,
    semantic: float | None,
    salary: float | None,
    soft: float | None,
) -> float:
    return normalize_components(
        {"semantic": (clamp(semantic), 0.50), "salary": (clamp(salary), 0.30), "soft": (clamp(soft), 0.20)}
    )


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
    return clamp(
        (
            match_weight * match
            + quality_weight * quality
            + freshness_weight * freshness
            + exposure_weight * exposure
        ) / total
    ) or 0.5


@dataclass
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
    salary = salary_fit_score(direction, criteria, candidate)
    preferences = extract_recommendation_v1_soft_preferences(criteria, direction)
    soft = soft_preference_score(preferences, candidate)
    quality = quality_score(candidate, direction)
    freshness = freshness_score(_value(candidate, "created_at"), now)
    scored = ScoredCandidate(
        candidate_id=str(candidate_id),
        owner_userid=owner_userid,
        data=candidate,
        semantic_score=clamp(semantic) or 0.5,
        salary_score=salary,
        soft_score=soft,
        quality_score=quality,
        freshness_score=freshness,
        exposure_count=int(exposure_count or 0),
        exposure_opportunity=clamp(exposure_opportunity) or 0.5,
    )
    scored.match_score = match_score(
        semantic=scored.semantic_score, salary=salary, soft=soft
    )
    scored.base_score = base_score(
        match=scored.match_score,
        quality=scored.quality_score,
        freshness=scored.freshness_score,
        exposure=scored.exposure_opportunity,
    )
    scored.repeat_adjusted_score = scored.base_score
    return scored
