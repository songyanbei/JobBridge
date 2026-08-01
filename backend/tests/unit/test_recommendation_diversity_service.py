"""§6.9-§6.10 diversity-selection regression tests."""
from __future__ import annotations

from app.services.recommendation_diversity_service import rank_candidates
from app.services.recommendation_scoring_service import ScoredCandidate


def _candidate(candidate_id: str, score: float, owner: str) -> ScoredCandidate:
    return ScoredCandidate(
        candidate_id=candidate_id,
        owner_userid=owner,
        match_score=score,
        base_score=score,
        repeat_adjusted_score=score,
        repeat_factor=1.0,
    )


def test_opened_repeat_bucket_is_not_truncated_in_sql_input_order():
    candidates = [
        _candidate("1", 0.86, "owner-1"),
        _candidate("2", 0.87, "owner-2"),
        _candidate("3", 0.88, "owner-3"),
        _candidate("4", 0.99, "owner-4"),
    ]

    ranked = rank_candidates(
        candidates,
        target=3,
        configured_owner_limit=1,
        diversity_level="low",
        exploration_percentage=0,
        userid="viewer",
        direction="search_job",
        query_digest="digest",
        strategy_version="recommendation-v1",
        rotation_date="2026-07-27",
    )

    assert ranked[0].candidate_id == "4"
    assert {item.candidate_id for item in ranked[:3]} == {"2", "3", "4"}


def test_owner_limit_relaxes_only_when_needed_to_fill_target():
    candidates = [
        _candidate("1", 0.99, "same-owner"),
        _candidate("2", 0.98, "same-owner"),
        _candidate("3", 0.97, "same-owner"),
    ]

    ranked = rank_candidates(
        candidates,
        target=3,
        configured_owner_limit=1,
        exploration_percentage=0,
        userid="viewer",
        direction="search_job",
        query_digest="digest",
        strategy_version="recommendation-v1",
        rotation_date="2026-07-27",
    )

    assert len(ranked[:3]) == 3
    assert all("constraint_relaxed" in item.reason_codes for item in ranked[:3])
