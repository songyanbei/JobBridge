"""Request-level ranking orchestration used by search and simulation."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from app.schemas.recommendation import RecommendationItem, RecommendationScoreDetail, RecommendationStrategyParameters
from app.services.recommendation_diversity_service import apply_repeat_factor, rank_candidates
from app.services.recommendation_scoring_service import (
    ScoredCandidate,
    build_scored_candidate,
    pre_score,
    semantic_scores,
    stable_hash_hex,
)


def precision_pool(
    candidate_dicts: list[Mapping[str, Any]],
    *,
    direction: str,
    criteria: Mapping[str, Any],
    userid: str,
    query_digest: str,
) -> list[str]:
    scored = []
    for candidate in candidate_dicts:
        salary = None
        try:
            from app.services.recommendation_scoring_service import salary_fit_score, quality_score, soft_preference_score, extract_recommendation_v1_soft_preferences
            salary = salary_fit_score(direction, criteria, candidate)
            soft = soft_preference_score(extract_recommendation_v1_soft_preferences(criteria, direction), candidate)
            quality = quality_score(candidate, direction)
        except Exception:
            soft = None
            quality = None
        scored.append((
            pre_score(salary=salary, soft=soft, quality=quality),
            stable_hash_hex(userid, direction, query_digest, "recommendation-v1", candidate.get("id")),
            str(candidate.get("id")),
        ))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in scored[:20]]


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
    snapshot_shown_ids: set[str] | None = None,
    rotation_date: str = "",
    now: datetime | None = None,
) -> tuple[list[ScoredCandidate], list[RecommendationItem]]:
    params = (
        RecommendationStrategyParameters.from_template("balanced")
        if parameters is None
        else RecommendationStrategyParameters.model_validate(parameters)
    )
    candidate_ids = [str(item.get("id")) for item in candidate_dicts]
    precision_ids = set(precision_pool(
        candidate_dicts, direction=direction, criteria=criteria,
        userid=userid, query_digest=query_digest,
    ))
    semantic = semantic_scores(
        precision_ids,
        semantic_ranked_items,
        failed=semantic_ranked_items is None,
    )
    counts = dict(exposure_counts or {})
    scored: list[ScoredCandidate] = []
    for candidate in candidate_dicts[:50]:
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
    ordered_counts = {item.candidate_id: item.exposure_count for item in scored}
    from app.services.recommendation_exposure_service import exposure_opportunities
    opportunities = exposure_opportunities(ordered_counts, candidate_ids)
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
        target=min(3, len(scored)),
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
    top = ordered[:3]
    items = [
        RecommendationItem(
            target_type="job" if direction == "search_job" else "resume",
            target_id=int(item.candidate_id),
            position=index,
            owner_userid=item.owner_userid,
            final_score=max(0.0, min(1.0, item.repeat_adjusted_score)),
            is_exploration=item.is_exploration,
            reason_codes=item.reason_codes,
            score_detail=RecommendationScoreDetail(
                match_score=item.match_score,
                quality_score=item.quality_score,
                freshness_score=item.freshness_score,
                exposure_opportunity=item.exposure_opportunity,
                base_score=item.base_score,
                repeat_factor=item.repeat_factor,
                repeat_adjusted_score=item.repeat_adjusted_score,
                is_exploration=item.is_exploration,
                reason_codes=item.reason_codes,
            ),
        )
        for index, item in enumerate(top, start=1)
    ]
    return ordered, items
