"""Request-level ranking orchestration used by search and simulation."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from app.schemas.recommendation import RecommendationItem, RecommendationScoreDetail, RecommendationStrategyParameters
from app.services.recommendation_diversity_service import apply_repeat_factor, rank_candidates
from app.services.recommendation_scoring_service import (
    V1_ALGORITHM_VERSION,
    V1_DISPLAY_TOP_N,
    V1_MAX_CANDIDATES,
    V1_PRECISION_POOL_SIZE,
    ScoredCandidate,
    build_scored_candidate,
    extract_recommendation_v1_soft_preferences,
    pre_score,
    quality_score,
    salary_fit_score,
    semantic_scores,
    soft_preference_score,
    stable_hash_hex,
)


def recommendation_score_detail(
    candidate: ScoredCandidate,
) -> RecommendationScoreDetail:
    """Project one scored candidate through the public score-detail contract.

    Snapshot persistence and the served ``RecommendationItem`` must use the
    same projection.  Keeping a hand-written snapshot field list beside the
    DTO previously let ``repeat_adjusted_score`` disappear while an internal
    ``diversity_penalty`` field leaked into Redis.
    """
    return RecommendationScoreDetail(
        match_score=candidate.match_score,
        quality_score=candidate.quality_score,
        freshness_score=candidate.freshness_score,
        exposure_opportunity=candidate.exposure_opportunity,
        base_score=candidate.base_score,
        repeat_factor=candidate.repeat_factor,
        repeat_adjusted_score=candidate.repeat_adjusted_score,
        is_exploration=candidate.is_exploration,
        reason_codes=list(candidate.reason_codes),
    )


def snapshot_candidate_scores(
    candidates: list[ScoredCandidate],
) -> dict[str, dict[str, Any]]:
    """Build the complete, Redis-safe score map for a ranked snapshot."""
    return {
        candidate.candidate_id: {
            "final_score": max(
                0.0, min(1.0, candidate.repeat_adjusted_score),
            ),
            "is_exploration": candidate.is_exploration,
            "reason_codes": list(candidate.reason_codes),
            "score_detail": recommendation_score_detail(candidate).model_dump(
                mode="json",
            ),
        }
        for candidate in candidates
    }


def precision_pool(
    candidate_dicts: list[Mapping[str, Any]],
    *,
    direction: str,
    criteria: Mapping[str, Any],
    userid: str,
    query_digest: str,
) -> list[str]:
    """§6.2.1 deterministic pre-scoring.

    Sort key is `(-pre_score, pre_tie_hash)` where the hash is compared as an
    ascending hex string, so the Top-20 is fully reproducible for a fixed
    (viewer, query digest, candidate set).
    """
    scored = []
    for candidate in candidate_dicts:
        try:
            salary = salary_fit_score(direction, criteria, candidate)
            soft = soft_preference_score(
                extract_recommendation_v1_soft_preferences(criteria, direction), candidate,
            )
            quality = quality_score(candidate, direction)
        except Exception:
            salary = soft = quality = None
        scored.append((
            pre_score(salary=salary, soft=soft, quality=quality),
            stable_hash_hex(userid, direction, query_digest, V1_ALGORITHM_VERSION, candidate.get("id")),
            str(candidate.get("id")),
        ))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in scored[:V1_PRECISION_POOL_SIZE]]


def rank_candidate_dicts(
    candidate_dicts: list[Mapping[str, Any]],
    *,
    direction: str,
    criteria: Mapping[str, Any],
    userid: str,
    query_digest: str,
    strategy_version: str | int = "recommendation-v1",
    parameters: RecommendationStrategyParameters | Mapping[str, Any] | None = None,
    semantic_ranked_items: list[Mapping[str, Any]] | None = None,
    exposure_counts: Mapping[str, int] | None = None,
    recent_exposures: Mapping[str, Any] | None = None,
    exposure_available: bool = True,
    precision_pool_ids: list[str] | None = None,
    snapshot_shown_ids: set[str] | None = None,
    rotation_date: str = "",
    now: datetime | None = None,
) -> tuple[list[ScoredCandidate], list[RecommendationItem]]:
    params = (
        RecommendationStrategyParameters.from_template("balanced")
        if parameters is None
        else RecommendationStrategyParameters.model_validate(parameters)
    )
    pool = list(candidate_dicts[:V1_MAX_CANDIDATES])
    candidate_ids = [str(item.get("id")) for item in pool]
    # The caller normally already computed the precision pool to build the LLM
    # request; recomputing it here would double the deterministic pre-scoring
    # work and risk the two copies drifting apart.
    precision_ids = list(precision_pool_ids) if precision_pool_ids is not None else precision_pool(
        pool, direction=direction, criteria=criteria,
        userid=userid, query_digest=query_digest,
    )
    semantic = semantic_scores(
        precision_ids,
        semantic_ranked_items,
        failed=semantic_ranked_items is None,
    )
    counts = dict(exposure_counts or {})
    scored: list[ScoredCandidate] = []
    for candidate in pool:
        cid = str(candidate.get("id"))
        item = build_scored_candidate(
            candidate,
            candidate_id=cid,
            owner_userid=candidate.get("owner_userid") or candidate.get("userid") or candidate.get("owner_id"),
            direction=direction,
            criteria=criteria,
            semantic=semantic.get(cid, 0.5),
            exposure_opportunity=0.5,
            exposure_count=counts.get(cid, 0),
            now=now,
        )
        scored.append(item)
    # Convert exposure counts into the within-pool percentile opportunity.
    # §6.6: when the exposure service is unavailable every candidate keeps the
    # strictly neutral 0.5 so the relative order is unchanged.
    from app.services.recommendation_exposure_service import exposure_opportunities
    opportunities = (
        exposure_opportunities({item.candidate_id: item.exposure_count for item in scored}, candidate_ids)
        if exposure_available
        else {cid: 0.5 for cid in candidate_ids}
    )
    for item in scored:
        item.exposure_opportunity = opportunities.get(item.candidate_id, 0.5)
        item.base_score = (
            params.match_weight * item.match_score
            + params.quality_weight * item.quality_score
            + params.freshness_weight * item.freshness_score
            + params.exposure_weight * item.exposure_opportunity
        ) / 100
    apply_repeat_factor(
        scored,
        recent_exposures=recent_exposures or {},
        cooldown_hours=params.repeat_cooldown_hours,
        snapshot_shown_ids=snapshot_shown_ids,
        now=now,
    )
    ordered = rank_candidates(
        scored,
        target=min(V1_DISPLAY_TOP_N, len(scored)),
        configured_owner_limit=params.same_owner_top_n_limit,
        diversity_level=params.diversity_level.value,
        exploration_percentage=params.exploration_percentage,
        userid=userid,
        direction=direction,
        query_digest=query_digest,
        strategy_version=strategy_version,
        rotation_date=rotation_date,
        snapshot_shown_ids=snapshot_shown_ids,
    )
    top = ordered[:V1_DISPLAY_TOP_N]
    items = [
        RecommendationItem(
            target_type="job" if direction == "search_job" else "resume",
            target_id=int(item.candidate_id),
            position=index,
            owner_userid=item.owner_userid,
            final_score=max(0.0, min(1.0, item.repeat_adjusted_score)),
            is_exploration=item.is_exploration,
            reason_codes=item.reason_codes,
            score_detail=recommendation_score_detail(item),
        )
        for index, item in enumerate(top, start=1)
    ]
    return ordered, items
