"""Deterministic repeat control, diversity and exploration for v1.

The ordering contract is §6.8-§6.10.1 of the v0.7 plan.  The stage order is
fixed: permanent exclusion → quality layering → cumulative repeat-bucket
opening → owner limit + greedy diversity → single exploration slot.
"""
from __future__ import annotations

import unicodedata
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from app.services.recommendation_scoring_service import (
    MIN_EXPLORATION_MATCH_RATIO,
    ScoredCandidate,
    bucket_hit,
    stable_hash_hex,
)

REPEAT_FACTORS = (("unseen", 1.0), ("late", 0.85), ("middle", 0.60), ("recent", 0.35))

# §6.9.5: selection scores within this distance are considered tied and are
# broken by the stable hash rather than by float ordering noise.
TIE_EPSILON = 1e-9


def diversity_lambda(level: str) -> float:
    return {"low": 0.05, "medium": 0.10, "high": 0.15}.get(str(level), 0.10)


def _norm(value: Any) -> str | None:
    """Same normalization as the scoring side (§6.3.4): NFKC, trim, collapse
    whitespace, lowercase.  Keeping the two in sync avoids full-width/half-width
    strings comparing equal in one layer and unequal in the other."""
    if value is None:
        return None
    value = unicodedata.normalize("NFKC", str(value)).strip()
    value = " ".join(value.split()).lower()
    return value or None


def _salary_bucket(candidate: ScoredCandidate, direction: str) -> int | None:
    """§6.9.1 jobs use the effective midpoint; §6.9.2 resumes use the expected
    floor.  Both bucket into 1000-yuan bands."""
    if direction == "search_job":
        floor = candidate.data.get("salary_floor_monthly")
        ceiling = candidate.data.get("salary_ceiling_monthly")
        try:
            mid = (float(floor) + float(ceiling if ceiling is not None else floor)) / 2
        except (TypeError, ValueError):
            return None
    else:
        try:
            mid = float(candidate.data.get("salary_expect_floor_monthly"))
        except (TypeError, ValueError):
            return None
    return int(mid // 1000) * 1000


def _combo(candidate: ScoredCandidate, *fields: str) -> tuple | None:
    """§6.9.3: a combination dimension is undecidable unless every member is
    present.  Returning a `(None, None)` tuple would otherwise compare equal to
    another `(None, None)` and register as perfect similarity."""
    values = tuple(candidate.data.get(name) for name in fields)
    if any(value is None for value in values):
        return None
    return values


def pair_similarity(a: ScoredCandidate, b: ScoredCandidate, direction: str) -> float:
    if direction == "search_job":
        dimensions = [
            (0.30, _norm(a.data.get("district")), _norm(b.data.get("district")), "eq"),
            (0.30, _salary_bucket(a, direction), _salary_bucket(b, direction), "eq"),
            (0.25, _norm(a.data.get("shift_pattern")), _norm(b.data.get("shift_pattern")), "eq"),
            (
                0.15,
                _combo(a, "provide_meal", "provide_housing"),
                _combo(b, "provide_meal", "provide_housing"),
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
            (0.30, jaccard, jaccard, "numeric"),
            (0.30, _salary_bucket(a, direction), _salary_bucket(b, direction), "eq"),
            (
                0.20,
                _combo(a, "accept_long_term", "accept_short_term"),
                _combo(b, "accept_long_term", "accept_short_term"),
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
        if candidate.candidate_id in shown:
            candidate.repeat_factor = 0.0
            candidate.repeat_bucket = "snapshot_shown"
            candidate.repeat_adjusted_score = 0.0
            continue
        candidate.repeat_factor = 1.0
        candidate.repeat_bucket = "unseen"
        last = recent_exposures.get(candidate.candidate_id)
        if last is None or cooldown_hours <= 0:
            candidate.repeat_adjusted_score = candidate.base_score
            continue
        try:
            if isinstance(last, datetime):
                hours = max((now - last.astimezone(timezone.utc)).total_seconds() / 3600, 0)
            else:
                hours = float(last)
        except (TypeError, ValueError):
            hours = cooldown_hours
        third = cooldown_hours / 3
        if hours < third:
            candidate.repeat_bucket, candidate.repeat_factor = "recent", 0.35
        elif hours < 2 * third:
            candidate.repeat_bucket, candidate.repeat_factor = "middle", 0.60
        elif hours < cooldown_hours:
            candidate.repeat_bucket, candidate.repeat_factor = "late", 0.85
        else:
            candidate.repeat_bucket, candidate.repeat_factor = "unseen", 1.0
        candidate.repeat_adjusted_score = candidate.base_score * candidate.repeat_factor


def _tie(candidate: ScoredCandidate, *, userid: str, direction: str, query_digest: str, strategy_version: str, rotation_date: str) -> str:
    return stable_hash_hex(userid, direction, query_digest, strategy_version, rotation_date, candidate.candidate_id)


def _minimum_buckets(candidates: list[ScoredCandidate], needed: int) -> list[ScoredCandidate]:
    """§6.10.1 step 2: open repeat buckets cumulatively (unseen → late → middle
    → recent) until the number of distinct candidates reaches `needed`, then
    return **every** candidate of the opened buckets.

    Truncating to exactly `needed` in input order would hand the greedy a pool
    already cut down to the first N rows of the SQL `created_at DESC` result, so
    the highest-scoring candidate could be discarded before ranking ever runs.
    """
    result: list[ScoredCandidate] = []
    for _, factor in REPEAT_FACTORS:
        result.extend(candidate for candidate in candidates if candidate.repeat_factor == factor)
        if len(result) >= needed:
            break
    return result


def _pick_best(
    items: list[ScoredCandidate],
    score_of: Callable[[ScoredCandidate], float],
    tie_of: Callable[[ScoredCandidate], Any],
) -> ScoredCandidate:
    """Highest score wins; anything within TIE_EPSILON of the leader is resolved
    by `tie_of` (§6.9.5), which ultimately ends in the ascending stable hash."""
    scores = {id(item): score_of(item) for item in items}
    best = max(scores.values())
    tied = [item for item in items if best - scores[id(item)] < TIE_EPSILON]
    return min(tied, key=tie_of)


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
    # 1. Permanent exclusion and quality layering.
    shown = snapshot_shown_ids or set()
    candidates = [
        candidate for candidate in candidates
        if candidate.candidate_id not in shown and not candidate.hard_filter_contract_broken
    ]
    if not candidates:
        return []
    target = min(target, len(candidates))
    best_match = max(candidate.match_score for candidate in candidates)
    quality_line = best_match * MIN_EXPLORATION_MATCH_RATIO
    near: list[ScoredCandidate] = []
    ordinary: list[ScoredCandidate] = []
    for candidate in candidates:
        if candidate.match_score >= quality_line:
            candidate.layer = "near"
            near.append(candidate)
        else:
            candidate.layer = "ordinary"
            ordinary.append(candidate)

    # 2-3. Cumulative repeat-bucket opening, near layer first.
    allowed = _minimum_buckets(near, target)
    if len(allowed) < target:
        allowed = allowed + _minimum_buckets(ordinary, target - len(allowed))
    allowed_ids = {id(candidate) for candidate in allowed}

    tie = lambda candidate: _tie(
        candidate, userid=userid, direction=direction, query_digest=query_digest,
        strategy_version=strategy_version, rotation_date=rotation_date,
    )
    lam = diversity_lambda(diversity_level)

    # 4. Owner limit + greedy diversity.  Every relaxation round restarts from an
    # empty selection (§6.10.1).
    def greedy(limit: int) -> tuple[list[ScoredCandidate], dict[int, float]]:
        selected: list[ScoredCandidate] = []
        penalties: dict[int, float] = {}
        remaining = list(allowed)
        while remaining and len(selected) < target:
            owner_counts: dict[str, int] = {}
            for item in selected:
                if item.owner_userid is not None:
                    owner_counts[item.owner_userid] = owner_counts.get(item.owner_userid, 0) + 1
            viable = [
                candidate for candidate in remaining
                if candidate.owner_userid is None
                or owner_counts.get(candidate.owner_userid, 0) < limit
            ]
            if not viable:
                break
            if not selected:
                chosen = _pick_best(viable, lambda c: c.repeat_adjusted_score, tie)
                penalties[id(chosen)] = 0.0
            else:
                penalty_of = {
                    id(c): lam * max_similarity(c, selected, direction) for c in viable
                }
                chosen = _pick_best(
                    viable,
                    lambda c: c.repeat_adjusted_score - penalty_of[id(c)],
                    tie,
                )
                penalties[id(chosen)] = penalty_of[id(chosen)]
            selected.append(chosen)
            remaining.remove(chosen)
        return selected, penalties

    configured_owner_limit = max(1, int(configured_owner_limit or 1))
    final_owner_limit = min(configured_owner_limit, target)
    selected: list[ScoredCandidate] = []
    penalties: dict[int, float] = {}
    for owner_limit in range(final_owner_limit, target + 1):
        selected, penalties = greedy(owner_limit)
        final_owner_limit = owner_limit
        if len(selected) == target or owner_limit == target:
            break
    constraint_relaxed = final_owner_limit > min(configured_owner_limit, target)
    for item in selected:
        item.diversity_penalty = penalties.get(id(item), 0.0)
        if constraint_relaxed:
            item.reason_codes.append("constraint_relaxed")

    # 5. Exploration replaces the third slot only.
    displaced: ScoredCandidate | None = None
    if exploration_percentage and len(selected) == 3 and bucket_hit(
        exploration_percentage, userid, direction, query_digest, strategy_version, rotation_date
    ):
        head = selected[:2]
        head_owner_counts: dict[str, int] = {}
        for item in head:
            if item.owner_userid is not None:
                head_owner_counts[item.owner_userid] = head_owner_counts.get(item.owner_userid, 0) + 1
        pool = [
            candidate for candidate in candidates
            if candidate.match_score >= quality_line
            and candidate.repeat_factor == 1.0
            and id(candidate) in allowed_ids
            and candidate not in head
            and (
                candidate.owner_userid is None
                or head_owner_counts.get(candidate.owner_userid, 0) + 1 <= final_owner_limit
            )
        ]
        if pool:
            exploration_penalty = {
                id(c): lam * max_similarity(c, head, direction) for c in pool
            }
            # §6.10.1 sort key: (-exploration_selection_score,
            # -repeat_adjusted_score, tie_hash).
            explorer = _pick_best(
                pool,
                lambda c: c.exposure_opportunity - exploration_penalty[id(c)],
                lambda c: (-c.repeat_adjusted_score, tie(c)),
            )
            explorer.is_exploration = True
            explorer.diversity_penalty = exploration_penalty[id(explorer)]
            if explorer is not selected[2]:
                displaced = selected[2]
                selected[2] = explorer

    # §6.10.1: the remainder keeps a deterministic order, and a third slot that
    # was displaced by exploration becomes its first entry.
    selected_ids = {id(item) for item in selected}
    remainder = [candidate for candidate in candidates if id(candidate) not in selected_ids]
    remainder.sort(key=lambda c: (-c.repeat_adjusted_score, tie(c)))
    if displaced is not None:
        remainder = [displaced] + [c for c in remainder if c is not displaced]
    return selected + remainder
