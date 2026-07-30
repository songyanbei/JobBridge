"""Side-effect-free strategy comparison using the production ranking pipeline."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models import RecommendationStrategyRelease, RecommendationStrategyVersion
from app.schemas.recommendation import RecommendationItem


@dataclass(frozen=True)
class SimulationResult:
    candidates: list[dict]
    current_items: list[RecommendationItem]
    draft_items: list[RecommendationItem]
    current_basis: str
    exposure_available: bool
    rotation_date: str
    llm_invoked: bool
    semantic_source: str
    simulation_mode: str
    llm_input_tokens: int | None = None
    llm_output_tokens: int | None = None


def _legacy_baseline(
    candidates: list[dict],
    direction: str,
    top_n: int,
    ranked_items: list[dict] | None = None,
) -> list[RecommendationItem]:
    """Project the actual legacy order, supplementing IDs omitted by the LLM.

    This mirrors the serving path: the provider may return fewer than ``top_n``
    IDs (or fail and return none), after which candidates keep their stable SQL
    order.  Calling this helper without ``ranked_items`` remains the deterministic
    failure fallback used by older callers.
    """
    by_id = {str(candidate.get("id")): candidate for candidate in candidates}
    ordered_ids: list[str] = []
    for item in ranked_items or []:
        candidate_id = str(item.get("id", ""))
        if candidate_id in by_id and candidate_id not in ordered_ids:
            ordered_ids.append(candidate_id)
    for candidate in candidates:
        candidate_id = str(candidate.get("id"))
        if candidate_id not in ordered_ids:
            ordered_ids.append(candidate_id)
    ordered = [by_id[candidate_id] for candidate_id in ordered_ids[:top_n]]
    return [
        RecommendationItem(
            target_type="job" if direction == "search_job" else "resume",
            target_id=int(candidate.get("id")),
            position=index,
            owner_userid=candidate.get("owner_userid"),
            final_score=0.0,
            is_exploration=False,
            reason_codes=["legacy_baseline"],
            score_detail=None,
        )
        for index, candidate in enumerate(ordered, start=1)
    ]


def simulate_strategy(
    db: Session,
    *,
    draft: RecommendationStrategyVersion,
    direction: str,
    user_id: str | None,
    raw_query: str,
    criteria: dict[str, Any],
) -> SimulationResult:
    """Compare stable/legacy with a draft without writing serving facts.

    Candidate lookup, exposure reads, precision-pool construction, semantic
    rerank and deterministic diversity ranking are the same functions used by
    the serving path.  The one LLM result is shared by stable and draft because
    the prompt/candidate pool is strategy-independent; this keeps the comparison
    controlled instead of introducing provider sampling noise between columns.
    """
    from app.core.time_utils import rotation_date, utc_now
    from app.services import search_service
    from app.services.recommendation_exposure_service import (
        batch_candidate_exposures,
        recent_user_exposures,
    )
    from app.services.recommendation_request_service import (
        precision_pool,
        rank_candidate_dicts,
    )
    from app.services.recommendation_scoring_service import (
        V1_DISPLAY_TOP_N,
        V1_MAX_CANDIDATES,
    )
    from app.services.intent_service import (
        _normalize_city_value,
        _normalize_job_category_value,
    )

    # Admin simulation bypasses the dialogue intent layer, so normalize the
    # form values here exactly as a production search would.  The UI commonly
    # submits short city names such as "苏州" while jobs store "苏州市".
    criteria = dict(criteria or {})
    if criteria.get("city"):
        cities = criteria["city"]
        cities = cities if isinstance(cities, list) else [cities]
        criteria["city"] = [
            _normalize_city_value(value) or value for value in cities
        ]
    if criteria.get("job_category"):
        categories = criteria["job_category"]
        categories = categories if isinstance(categories, list) else [categories]
        criteria["job_category"] = [
            _normalize_job_category_value(value) or value
            for value in categories
        ]

    release = db.get(RecommendationStrategyRelease, direction)
    stable = (
        db.get(RecommendationStrategyVersion, release.stable_version_id)
        if release and release.stable_version_id else None
    )
    legacy_max_candidates = search_service._get_config_int(
        "match.max_candidates", db, 50,
    )
    legacy_top_n = search_service._get_config_int("match.top_n", db, 3)
    query_limit = (
        V1_MAX_CANDIDATES
        if stable is not None
        else max(V1_MAX_CANDIDATES, legacy_max_candidates)
    )

    if direction == "search_job":
        candidates = search_service._jobs_to_dicts(
            search_service._query_jobs(criteria, query_limit, db),
            db,
        )
        target_type = "job"
        role = "worker"
    else:
        candidates = search_service._resumes_to_dicts(
            search_service._query_resumes(criteria, query_limit, db),
        )
        target_type = "resume"
        role = "factory"

    # Draft/stable v1 is contractually capped at the latest 50 even when the
    # current legacy configuration asks the shared SQL query for a larger pool.
    v1_candidates = candidates[:V1_MAX_CANDIDATES]
    simulated_user = user_id or "simulation"
    query_digest = search_service.conversation_service.compute_query_digest(criteria)
    scoring_time = utc_now()
    candidate_ids = [
        str(item.get("id")) for item in v1_candidates
    ]
    draft_params = draft.parameters or {}

    try:
        exposure_counts = batch_candidate_exposures(
            db,
            target_type=target_type,
            candidate_ids=candidate_ids,
            request_now_utc=scoring_time,
        )
        recent_exposures = recent_user_exposures(
            db,
            viewer_userid=simulated_user,
            target_type=target_type,
            candidate_ids=candidate_ids,
            request_now_utc=scoring_time,
            cooldown_hours=int(draft_params.get("repeat_cooldown_hours", 24) or 0),
        )
        exposure_available = True
    except Exception:
        exposure_counts, recent_exposures, exposure_available = {}, {}, False

    precision_ids = precision_pool(
        v1_candidates,
        direction=direction,
        criteria=criteria,
        userid=simulated_user,
        query_digest=query_digest,
    )
    candidates_by_id = {
        str(candidate.get("id")): candidate for candidate in v1_candidates
    }
    precision_candidates = [
        candidates_by_id[candidate_id]
        for candidate_id in precision_ids
        if candidate_id in candidates_by_id
    ]

    llm_invoked = bool(precision_candidates)
    llm_result = None
    if llm_invoked:
        query = raw_query.strip() or json.dumps(
            criteria, ensure_ascii=False, sort_keys=True, default=str,
        )
        llm_result = search_service._rerank_with_logging(
            query=query,
            candidates=precision_candidates,
            role=role,
            top_n=V1_DISPLAY_TOP_N,
            call_site="recommendation_simulation",
        )
    semantic_ranked_items = llm_result.ranked_items if llm_result is not None else None
    semantic_source = (
        "llm" if llm_result is not None and llm_result.ranked_items
        else "llm_fallback_neutral" if llm_invoked
        else "no_candidates"
    )
    mode = "llm" if semantic_source == "llm" else (
        "deterministic_fallback" if llm_invoked else "no_candidates"
    )

    common = {
        "direction": direction,
        "criteria": criteria,
        "userid": simulated_user,
        "query_digest": query_digest,
        "semantic_ranked_items": semantic_ranked_items,
        "exposure_counts": exposure_counts,
        "recent_exposures": recent_exposures,
        "exposure_available": exposure_available,
        "precision_pool_ids": precision_ids,
        "rotation_date": rotation_date(scoring_time),
        "now": scoring_time,
    }
    _draft_ranked, draft_items = rank_candidate_dicts(
        v1_candidates,
        strategy_version=draft.id,
        parameters=draft_params,
        **common,
    )

    legacy_llm_result = None
    if stable is not None:
        _stable_ranked, current_items = rank_candidate_dicts(
            v1_candidates,
            strategy_version=stable.id,
            parameters=stable.parameters,
            **common,
        )
        current_basis = "stable"
    else:
        legacy_soft_preferences, legacy_ranking_weights = (
            search_service._extract_soft_prefs_for_rerank(
                criteria,
                "job_search" if direction == "search_job" else "candidate_search",
            )
        )
        legacy_llm_result = search_service._rerank_with_logging(
            query=raw_query.strip() or json.dumps(
                criteria, ensure_ascii=False, sort_keys=True, default=str,
            ),
            candidates=candidates[:legacy_max_candidates],
            role=role,
            top_n=legacy_top_n,
            call_site="recommendation_simulation",
            soft_preferences=legacy_soft_preferences,
            ranking_weights=legacy_ranking_weights,
        )
        current_items = _legacy_baseline(
            candidates[:legacy_max_candidates],
            direction,
            legacy_top_n,
            legacy_llm_result.ranked_items,
        )
        current_basis = "legacy"

    llm_results = [
        result for result in (llm_result, legacy_llm_result) if result is not None
    ]

    def _sum_usage(field: str) -> int | None:
        values = [
            getattr(result, field, None)
            for result in llm_results
            if getattr(result, field, None) is not None
        ]
        return sum(int(value) for value in values) if values else None

    return SimulationResult(
        candidates=candidates,
        current_items=current_items,
        draft_items=draft_items,
        current_basis=current_basis,
        exposure_available=exposure_available,
        rotation_date=rotation_date(scoring_time),
        llm_invoked=llm_invoked,
        semantic_source=semantic_source,
        simulation_mode=mode,
        llm_input_tokens=_sum_usage("input_tokens"),
        llm_output_tokens=_sum_usage("output_tokens"),
    )


__all__ = ["SimulationResult", "simulate_strategy"]
