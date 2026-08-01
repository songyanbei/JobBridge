"""Production ranking pipeline contracts exercised with real schema/config."""
from __future__ import annotations

import pytest

from app.services.recommendation_request_service import precision_pool, rank_candidate_dicts
from app.services.recommendation_scoring_service import V1_MAX_CANDIDATES

pytestmark = pytest.mark.integration


def test_precision_pool_is_bounded_and_rank_pipeline_returns_top_three():
    candidates = [
        {
            "id": index,
            "owner_userid": f"owner-{index % 2}",
            "salary_floor_monthly": 5000 + index,
            "salary_ceiling_monthly": 8000 + index,
            "created_at": "2026-07-27T00:00:00+00:00",
        }
        for index in range(1, 31)
    ]
    pool = precision_pool(
        candidates,
        direction="search_job",
        criteria={"city": ["苏州市"], "salary_floor_monthly": 6000},
        userid="integration-viewer",
        query_digest="integration-digest",
    )
    ordered, items = rank_candidate_dicts(
        candidates,
        direction="search_job",
        criteria={"city": ["苏州市"], "salary_floor_monthly": 6000},
        userid="integration-viewer",
        query_digest="integration-digest",
        strategy_version=7,
        precision_pool_ids=pool,
        semantic_ranked_items=[],
        exposure_counts={},
        recent_exposures={},
        now=None,
    )
    assert len(pool) <= 20
    assert len(candidates) <= V1_MAX_CANDIDATES
    assert len(ordered) == 30
    assert len(items) == 3
