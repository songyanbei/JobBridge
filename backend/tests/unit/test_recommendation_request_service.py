"""§6.2 / §6.3 request-level ranking orchestration tests."""
from __future__ import annotations

from app.services.recommendation_request_service import (
    precision_pool,
    rank_candidate_dicts,
)
from app.services.recommendation_scoring_service import V1_PRECISION_POOL_SIZE


def test_precision_pool_is_deterministic_and_capped_at_twenty():
    candidates = [
        {
            "id": value,
            "salary_ceiling_monthly": 8000 + value,
            "description": "完整描述",
        }
        for value in range(30)
    ]
    kwargs = {
        "direction": "search_job",
        "criteria": {"salary_floor_monthly": 5000},
        "userid": "viewer",
        "query_digest": "digest",
    }

    forward = precision_pool(candidates, **kwargs)
    reversed_input = precision_pool(list(reversed(candidates)), **kwargs)

    assert len(forward) == V1_PRECISION_POOL_SIZE
    assert forward == reversed_input


def test_hard_filter_contract_break_is_removed_from_ranked_output():
    candidates = [
        {
            "id": 1,
            "owner_userid": "owner-1",
            "salary_ceiling_monthly": 7000,
        },
        {
            "id": 2,
            "owner_userid": "owner-2",
            "salary_ceiling_monthly": 9000,
        },
    ]
    ordered, items = rank_candidate_dicts(
        candidates,
        direction="search_job",
        criteria={"salary_floor_monthly": 8000},
        userid="viewer",
        query_digest="digest",
        semantic_ranked_items=[],
        precision_pool_ids=["1", "2"],
        rotation_date="2026-07-27",
    )

    assert [item.candidate_id for item in ordered] == ["2"]
    assert [item.target_id for item in items] == [2]
