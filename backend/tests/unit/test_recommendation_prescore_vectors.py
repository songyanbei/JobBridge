"""§6.2.1 fixed pre-scoring vectors.

The plan pins these numbers as a contract: any change to the pre-score formula,
the component renormalization, the all-missing default or the tie-break hash
must bump `algorithm_version` and update this file deliberately.
"""
import pytest

from app.services.recommendation_request_service import precision_pool
from app.services.recommendation_scoring_service import (
    V1_ALGORITHM_VERSION,
    V1_PRECISION_POOL_SIZE,
    pre_score,
    stable_hash_hex,
)

TOLERANCE = 1e-4

VIEWER = "u100"
DIRECTION = "search_job"
QUERY_DIGEST = "qabc"

# candidate_id, salary, soft, quality, expected pre_score, expected rank (1-based)
VECTOR = [
    (101, 1.00, 0.80, 0.60, 0.9000, 2),
    (102, 0.90, None, 0.90, 0.9000, 1),
    (103, None, 0.80, 0.80, 0.8000, 3),
    (104, None, None, None, 0.5000, 4),
]


def _tie(candidate_id):
    return stable_hash_hex(VIEWER, DIRECTION, QUERY_DIGEST, V1_ALGORITHM_VERSION, candidate_id)


@pytest.mark.parametrize("candidate_id,salary,soft,quality,expected,_rank", VECTOR)
def test_pre_score_matches_fixed_vector(candidate_id, salary, soft, quality, expected, _rank):
    assert pre_score(salary=salary, soft=soft, quality=quality) == pytest.approx(
        expected, abs=TOLERANCE
    )


def test_missing_components_are_renormalized_not_zeroed():
    """102 has no soft-preference signal at all, yet still reaches 0.90.

    Treating the missing component as 0 would give
    (0.60*0.90 + 0.30*0 + 0.10*0.90) / 1.00 = 0.63.
    """
    assert pre_score(salary=0.90, soft=None, quality=0.90) == pytest.approx(0.9000, abs=TOLERANCE)
    assert pre_score(salary=0.90, soft=0.0, quality=0.90) == pytest.approx(0.6300, abs=TOLERANCE)


def test_all_components_missing_defaults_to_midpoint():
    assert pre_score(salary=None, soft=None, quality=None) == pytest.approx(0.5, abs=TOLERANCE)


def test_zero_component_is_not_rewritten_to_the_default():
    """`clamp(...) or default` silently turned a legitimate 0.0 into 0.5, which
    moved candidates into and out of the precision pool."""
    assert pre_score(salary=0.0, soft=0.0, quality=0.0) == pytest.approx(0.0, abs=TOLERANCE)


def test_tie_break_hash_matches_the_documented_digests():
    """The plan quotes these two prefixes; they are what makes 102 outrank 101."""
    assert _tie(101).startswith("59a7d71f")
    assert _tie(102).startswith("3298e376")
    assert _tie(102) < _tie(101)


def test_precision_pool_ordering_is_reproducible():
    """Equal pre_score must resolve by ascending hash, never by input order."""
    candidates = [
        {"id": 101, "salary_ceiling_monthly": 6000, "provide_meal": True},
        {"id": 102, "salary_ceiling_monthly": 5800, "provide_meal": None},
        {"id": 103, "salary_ceiling_monthly": None, "provide_meal": True},
        {"id": 104, "salary_ceiling_monthly": None, "provide_meal": None},
    ]
    criteria = {"salary_floor_monthly": 5000, "provide_meal": True}
    first = precision_pool(
        candidates, direction=DIRECTION, criteria=criteria,
        userid=VIEWER, query_digest=QUERY_DIGEST,
    )
    shuffled = precision_pool(
        list(reversed(candidates)), direction=DIRECTION, criteria=criteria,
        userid=VIEWER, query_digest=QUERY_DIGEST,
    )
    assert first == shuffled, "precision pool must not depend on SQL input order"


def test_precision_pool_is_capped_by_the_v1_constant():
    candidates = [{"id": i, "salary_ceiling_monthly": 6000} for i in range(60)]
    pool = precision_pool(
        candidates, direction=DIRECTION, criteria={"salary_floor_monthly": 5000},
        userid=VIEWER, query_digest=QUERY_DIGEST,
    )
    assert len(pool) == V1_PRECISION_POOL_SIZE


def test_pre_score_ignores_exposure_and_rotation_inputs():
    """§6.2.1: pre-scoring must not read exposure, freshness or rotation, so
    display feedback cannot change who is eligible for semantic reranking."""
    import inspect

    from app.services import recommendation_request_service as mod

    source = inspect.getsource(mod.precision_pool)
    for forbidden in ("exposure", "freshness", "rotation", "created_at"):
        assert forbidden not in source, f"precision_pool must not consult {forbidden}"
