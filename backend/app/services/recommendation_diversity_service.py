"""Deterministic repeat control, diversity and exploration for v1."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from app.services.recommendation_scoring_service import (
    MIN_EXPLORATION_MATCH_RATIO,
    ScoredCandidate,
    bucket_hit,
    stable_hash_hex,
)

REPEAT_FACTORS = (("unseen", 1.0), ("late", 0.85), ("middle", 0.60), ("recent", 0.35))


def diversity_lambda(level: str) -> float:
    return {"low": 0.05, "medium": 0.10, "high": 0.15}.get(str(level), 0.10)


def _norm(value: Any) -> str | None:
    if value is None:
        return None
    value = " ".join(str(value).strip().split()).lower()
    return value or None


def _salary_bucket(candidate: ScoredCandidate) -> int | None:
    floor = candidate.data.get("salary_floor_monthly")
    ceiling = candidate.data.get("salary_ceiling_monthly")
    try:
        mid = (float(floor) + float(ceiling if ceiling is not None else floor)) / 2
    except (TypeError, ValueError):
        return None
    return int(mid // 1000) * 1000


def pair_similarity(a: ScoredCandidate, b: ScoredCandidate, direction: str) -> float:
    if direction == "search_job":
        dimensions = [
            (0.30, _norm(a.data.get("district")), _norm(b.data.get("district")), "eq"),
            (0.30, _salary_bucket(a), _salary_bucket(b), "eq"),
            (0.25, _norm(a.data.get("shift_pattern")), _norm(b.data.get("shift_pattern")), "eq"),
            (
                0.15,
                (a.data.get("provide_meal"), a.data.get("provide_housing")),
                (b.data.get("provide_meal"), b.data.get("provide_housing")),
                "eq",
            ),
        ]
    else:
        districts_a = set(a.data.get("expected_districts") or [])
        districts_b = set(b.data.get("expected_districts") or [])
        jaccard = (
            len(districts_a & districts_b) / len(districts_a | districts_b)
            if districts_a and districts_b else None
        )
        dimensions = [
            (0.30, jaccard, 1.0, "numeric"),
            (0.30, _salary_bucket(a), _salary_bucket(b), "eq"),
            (
                0.20,
                (a.data.get("accept_long_term"), a.data.get("accept_short_term")),
                (b.data.get("accept_long_term"), b.data.get("accept_short_term")),
                "eq",
            ),
            (0.20, _norm(a.data.get("education")), _norm(b.data.get("education")), "eq"),
        ]
    numerator = denominator = 0.0
    for weight, left, right, kind in dimensions:
        if left is None or right is None:
            continue
        denominator += weight
        numerator += weight * (float(left) if kind == "numeric" else float(left == right))
    return numerator / denominator if denominator else 0.0


def max_similarity(candidate: ScoredCandidate, selected: list[ScoredCandidate], direction: str) -> float:
    return max((pair_similarity(candidate, item, direction) for item in selected), default=0.0)


def apply_repeat_factor(
    candidates: list[ScoredCandidate],
    *,
    recent_exposures: Mapping[str, float | int | None],
    cooldown_hours: int,
    snapshot_shown_ids: set[str] | None = None,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(timezone.utc)
    shown = snapshot_shown_ids or set()
    for candidate in candidates:
        candidate.repeat_factor = 0.0 if candidate.candidate_id in shown else 1.0
        last = recent_exposures.get(candidate.candidate_id)
        if last is None or candidate.repeat_factor == 0.0 or cooldown_hours <= 0:
            candidate.repeat_adjusted_score = candidate.base_score * candidate.repeat_factor
            continue
        try:
            if isinstance(last, datetime):
                hours = max((now - last.astimezone(timezone.utc)).total_seconds() / 3600, 0)
            else:
                hours = float(last)
        except (TypeError, ValueError):
            hours = cooldown_hours
        third = cooldown_hours / 3
        candidate.repeat_factor = 0.35 if hours < third else 0.60 if hours < 2 * third else 0.85 if hours < cooldown_hours else 1.0
        candidate.repeat_adjusted_score = candidate.base_score * candidate.repeat_factor


def _tie(candidate: ScoredCandidate, *, userid: str, direction: str, query_digest: str, strategy_version: str, rotation_date: str) -> str:
    return stable_hash_hex(userid, direction, query_digest, strategy_version, rotation_date, candidate.candidate_id)


def _minimum_buckets(candidates: list[ScoredCandidate], needed: int) -> list[ScoredCandidate]:
    result: list[ScoredCandidate] = []
    for _, factor in REPEAT_FACTORS:
        result.extend(candidate for candidate in candidates if candidate.repeat_factor == factor)
        if len(result) >= needed:
            break
    return result[:needed]


def rank_candidates(
    candidates: list[ScoredCandidate],
    *,
    target: int = 3,
    configured_owner_limit: int = 1,
    diversity_level: str = "medium",
    exploration_percentage: int = 0,
    userid: str = "",
    direction: str = "search_job",
    query_digest: str = "",
    strategy_version: str = "recommendation-v1",
    rotation_date: str = "",
    snapshot_shown_ids: set[str] | None = None,
) -> list[ScoredCandidate]:
    candidates = [candidate for candidate in candidates if candidate.candidate_id not in (snapshot_shown_ids or set())]
    if not candidates:
        return []
    target = min(target, len(candidates))
    best_match = max(candidate.match_score for candidate in candidates)
    near = [candidate for candidate in candidates if candidate.match_score >= best_match * MIN_EXPLORATION_MATCH_RATIO]
    ordinary = [candidate for candidate in candidates if candidate not in near]
    allowed = _minimum_buckets(near, target)
    if len(allowed) < target:
        allowed.extend(_minimum_buckets(ordinary, target - len(allowed)))
    tie = lambda candidate: _tie(candidate, userid=userid, direction=direction, query_digest=query_digest, strategy_version=strategy_version, rotation_date=rotation_date)
    lam = diversity_lambda(diversity_level)

    def greedy(limit: int) -> list[ScoredCandidate]:
        selected: list[ScoredCandidate] = []
        remaining = list(allowed)
        while remaining and len(selected) < target:
            if not selected:
                key = lambda c: (-c.repeat_adjusted_score, tie(c))
            else:
                key = lambda c: (
                    -(c.repeat_adjusted_score - lam * max_similarity(c, selected, direction)),
                    tie(c),
                )
            viable = [
                candidate for candidate in remaining
                if sum(item.owner_userid == candidate.owner_userid for item in selected if candidate.owner_userid is not None) < limit
            ]
            if not viable:
                break
            chosen = min(viable, key=key)
            selected.append(chosen)
            remaining.remove(chosen)
        return selected

    final_limit = max(1, min(configured_owner_limit, target))
    selected = greedy(final_limit)
    while len(selected) < target and final_limit < target:
        final_limit += 1
        selected = greedy(final_limit)

    if exploration_percentage and len(selected) == 3 and bucket_hit(
        exploration_percentage, userid, direction, query_digest, strategy_version, rotation_date
    ):
        pool = [
            candidate for candidate in candidates
            if candidate.match_score >= best_match * MIN_EXPLORATION_MATCH_RATIO
            and candidate.repeat_factor == 1.0
            and candidate not in selected[:2]
            and sum(item.owner_userid == candidate.owner_userid for item in selected[:2] if candidate.owner_userid is not None) + 1 <= final_limit
        ]
        if pool:
            explorer = min(
                pool,
                key=lambda c: (
                    -(c.exposure_opportunity - lam * max_similarity(c, selected[:2], direction)),
                    -c.repeat_adjusted_score,
                    tie(c),
                ),
            )
            if explorer is not selected[2]:
                displaced = selected[2]
                selected[2] = explorer
                selected[2].is_exploration = True
                candidates = [item for item in candidates if item is not explorer]
                candidates.insert(0, displaced)
            else:
                explorer.is_exploration = True
    selected_ids = {item.candidate_id for item in selected}
    remainder = [candidate for candidate in candidates if candidate.candidate_id not in selected_ids]
    remainder.sort(key=lambda c: (-c.repeat_adjusted_score, tie(c)))
    return selected + remainder

