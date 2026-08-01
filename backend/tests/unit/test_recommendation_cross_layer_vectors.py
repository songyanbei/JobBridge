"""§6.10.2 cross-layer vectors plus the ordering regressions they guard.

These are the tests that would have caught the two ranking P0s: the greedy pool
being truncated in SQL order, and the exploration slot losing the candidate it
displaced.
"""
import pytest

from app.services import recommendation_diversity_service as diversity
from app.services.recommendation_diversity_service import rank_candidates
from app.services.recommendation_scoring_service import ScoredCandidate

USER = "u1"
DIRECTION = "search_job"
DIGEST = "q"
VERSION = "1"
ROTATION = "2026-07-26"


def make(candidate_id, *, match, base, factor, owner=None, exposure=0.5, data=None):
    candidate = ScoredCandidate(
        candidate_id=candidate_id, owner_userid=owner, data=data or {},
    )
    candidate.match_score = match
    candidate.base_score = base
    candidate.repeat_factor = factor
    candidate.repeat_adjusted_score = base * factor
    candidate.exposure_opportunity = exposure
    return candidate


def rank(candidates, **kwargs):
    params = dict(
        target=3, configured_owner_limit=3, diversity_level="medium",
        exploration_percentage=0, userid=USER, direction=DIRECTION,
        query_digest=DIGEST, strategy_version=VERSION, rotation_date=ROTATION,
    )
    params.update(kwargs)
    return rank_candidates(candidates, **params)


@pytest.fixture
def flat_similarity(monkeypatch):
    """Force every pair similarity to 0 so a vector isolates layering."""
    monkeypatch.setattr(diversity, "pair_similarity", lambda a, b, d: 0.0)


@pytest.fixture
def injected_similarity(monkeypatch):
    """Let a vector state pair similarities directly, as the plan tables do."""
    table = {}

    def apply(pairs):
        table.clear()
        table.update(pairs)

    monkeypatch.setattr(
        diversity, "pair_similarity",
        lambda a, b, d: table.get(
            (a.candidate_id, b.candidate_id),
            table.get((b.candidate_id, a.candidate_id), 0.0),
        ),
    )
    return apply


def test_vector_a_near_layer_outranks_an_unseen_ordinary_candidate(flat_similarity):
    """§6.10.2 vector A.

    `near` only reaches three distinct candidates once the `recent` bucket
    opens, so B is served at 0.329 while D — unseen, ordinary, and the highest
    adjusted score in the whole pool at 0.990 — stays out of the Top 3.
    """
    candidates = [
        make("A", match=1.00, base=0.90, factor=1.00),
        make("E", match=0.88, base=0.80, factor=0.85),
        make("B", match=0.86, base=0.94, factor=0.35),
        make("D", match=0.84, base=0.99, factor=1.00),
    ]
    result = rank(candidates)
    assert [c.candidate_id for c in result[:3]] == ["A", "E", "B"]
    assert result[3].candidate_id == "D"


def test_vector_b_exploration_reruns_diversity_and_displaces_slot_three(injected_similarity):
    """§6.10.2 vector B.

    B is already in the top two so it is not moved. C scores 0.30-0.01=0.29 and
    D 0.95-0.02=0.93, so D takes the exploration slot and C becomes the first
    remaining candidate.
    """
    injected_similarity({("C", "A"): 0.10, ("C", "B"): 0.10,
                         ("D", "A"): 0.20, ("D", "B"): 0.20})
    candidates = [
        make("A", match=0.95, base=0.92, factor=1.0, owner="X", exposure=0.10),
        make("B", match=0.95, base=0.88, factor=1.0, owner="Y", exposure=0.99),
        make("C", match=0.95, base=0.86, factor=1.0, owner="X", exposure=0.30),
        make("D", match=0.95, base=0.82, factor=1.0, owner="Z", exposure=0.95),
    ]
    result = rank(candidates, configured_owner_limit=2, exploration_percentage=100)
    assert [c.candidate_id for c in result[:3]] == ["A", "B", "D"]
    assert result[2].is_exploration is True
    assert result[3].candidate_id == "C", "displaced slot 3 heads the remainder"


def test_exploration_skips_a_candidate_that_would_break_the_owner_limit(injected_similarity):
    """§6.10.2 closing paragraph: the best explorer that violates the final
    owner limit is skipped in favour of the next eligible one."""
    injected_similarity({})
    candidates = [
        make("A", match=0.95, base=0.92, factor=1.0, owner="X", exposure=0.10),
        make("B", match=0.95, base=0.88, factor=1.0, owner="Y", exposure=0.20),
        make("C", match=0.95, base=0.86, factor=1.0, owner="Z", exposure=0.30),
        # Highest exposure opportunity, but owner X already holds the only slot.
        make("D", match=0.95, base=0.82, factor=1.0, owner="X", exposure=0.99),
        make("E", match=0.95, base=0.80, factor=1.0, owner="W", exposure=0.90),
    ]
    result = rank(candidates, configured_owner_limit=1, exploration_percentage=100)
    top = [c.candidate_id for c in result[:3]]
    assert "D" not in top, "explorer must not break the final owner limit"
    assert top[2] == "E"


def test_exploration_with_no_eligible_candidate_keeps_the_normal_third(injected_similarity):
    """§6.10: quality is never sacrificed to hit an exploration ratio."""
    injected_similarity({})
    candidates = [
        make("A", match=0.95, base=0.92, factor=1.0, owner="X"),
        make("B", match=0.95, base=0.88, factor=1.0, owner="Y"),
        # Only remaining near candidate is a repeat, so it cannot explore.
        make("C", match=0.95, base=0.86, factor=0.35, owner="Z"),
    ]
    result = rank(candidates, configured_owner_limit=3, exploration_percentage=100)
    assert [c.candidate_id for c in result[:3]] == ["A", "B", "C"]
    assert result[2].is_exploration is False


def test_opened_buckets_are_returned_whole_not_truncated(flat_similarity):
    """Regression for the truncation bug.

    Five unseen candidates in SQL `created_at DESC` order with the best scores
    last. Cutting the pool to exactly TARGET in input order returned the three
    *newest* rows and threw away the top scorers entirely.
    """
    candidates = [make(str(i), match=0.95, base=0.5 + i * 0.05, factor=1.0) for i in range(5)]
    result = rank(candidates)
    assert [c.candidate_id for c in result[:3]] == ["4", "3", "2"]


def test_ordinary_layer_only_fills_when_near_is_exhausted(flat_similarity):
    """§6.10.1 enforces layer priority through *pool membership*, not ordering.

    Note the subtlety: §6.7's prose reads as though the ordinary layer is only
    ever appended after the near layer, but the §6.10.1 pseudocode — which the
    plan declares the normative contract ("任何实现不得交换阶段") — merges the
    ordinary supplements into a single `allowed` pool and runs one greedy over
    it. So once near cannot fill TARGET, a supplemented ordinary candidate may
    legitimately outrank the near one on score. What layering guarantees is that
    ordinary candidates are never *considered* while near can fill the slots —
    which is exactly what vector A pins down.
    """
    candidates = [
        make("N1", match=1.00, base=0.50, factor=1.0),
        make("O1", match=0.50, base=0.99, factor=1.0),
        make("O2", match=0.50, base=0.98, factor=1.0),
    ]
    result = rank(candidates)
    assert sorted(c.candidate_id for c in result[:3]) == ["N1", "O1", "O2"]
    assert [c.candidate_id for c in result[:3]] == ["O1", "O2", "N1"]

    # ...whereas a near layer that *can* fill TARGET excludes ordinary entirely,
    # however high the ordinary score is.
    near_full = [
        make("N1", match=1.00, base=0.50, factor=1.0),
        make("N2", match=0.95, base=0.49, factor=1.0),
        make("N3", match=0.90, base=0.48, factor=1.0),
        make("O1", match=0.50, base=0.99, factor=1.0),
    ]
    top = [c.candidate_id for c in rank(near_full)[:3]]
    assert top == ["N1", "N2", "N3"] and "O1" not in top


def test_tie_break_uses_the_stable_hash_within_the_epsilon(flat_similarity):
    """§6.9.5: scores differing by less than 1e-9 are a tie and must resolve by
    hash, not by float ordering noise or input order."""
    a = make("101", match=0.95, base=0.90, factor=1.0)
    b = make("102", match=0.95, base=0.90 + 1e-12, factor=1.0)
    forward = rank([a, b], target=2)
    backward = rank(
        [make("102", match=0.95, base=0.90 + 1e-12, factor=1.0),
         make("101", match=0.95, base=0.90, factor=1.0)],
        target=2,
    )
    assert [c.candidate_id for c in forward] == [c.candidate_id for c in backward]


def test_current_snapshot_items_are_never_repeated(flat_similarity):
    candidates = [
        make("A", match=0.95, base=0.99, factor=1.0),
        make("B", match=0.95, base=0.90, factor=1.0),
        make("C", match=0.95, base=0.80, factor=1.0),
    ]
    result = rank(candidates, snapshot_shown_ids={"A"})
    assert "A" not in [c.candidate_id for c in result]


def test_hard_filter_violations_are_dropped_from_the_result(flat_similarity):
    good = make("A", match=0.95, base=0.90, factor=1.0)
    bad = make("B", match=0.95, base=0.99, factor=1.0)
    bad.hard_filter_contract_broken = True
    result = rank([good, bad])
    assert [c.candidate_id for c in result] == ["A"]


def test_owner_limit_relaxation_restarts_and_is_recorded(flat_similarity):
    """§6.9.4: each relaxation round recomputes from an empty selection and
    records that the constraint was relaxed."""
    candidates = [
        make("A", match=0.95, base=0.90, factor=1.0, owner="X"),
        make("B", match=0.95, base=0.85, factor=1.0, owner="X"),
        make("C", match=0.95, base=0.80, factor=1.0, owner="X"),
    ]
    result = rank(candidates, configured_owner_limit=1)
    assert [c.candidate_id for c in result[:3]] == ["A", "B", "C"]
    assert all("constraint_relaxed" in c.reason_codes for c in result[:3])


def test_combination_dimension_with_missing_members_is_undecidable():
    """§6.9.3: `(None, None)` must drop out of the denominator instead of
    comparing equal and registering perfect similarity."""
    a = make("A", match=1.0, base=1.0, factor=1.0,
             data={"provide_meal": None, "provide_housing": None, "district": "甲"})
    b = make("B", match=1.0, base=1.0, factor=1.0,
             data={"provide_meal": None, "provide_housing": None, "district": "乙"})
    assert diversity.pair_similarity(a, b, "search_job") == pytest.approx(0.0)


def test_normalization_is_width_insensitive_like_the_scoring_side():
    a = make("A", match=1.0, base=1.0, factor=1.0, data={"district": "ＡＢＣ"})
    b = make("B", match=1.0, base=1.0, factor=1.0, data={"district": "abc"})
    assert diversity.pair_similarity(a, b, "search_job") == pytest.approx(1.0)


def test_resume_salary_bucket_reads_the_resume_column():
    """The job column name was being read for resumes, which silently collapsed
    worker similarity onto education alone."""
    a = make("A", match=1.0, base=1.0, factor=1.0, data={"salary_expect_floor_monthly": 5200})
    b = make("B", match=1.0, base=1.0, factor=1.0, data={"salary_expect_floor_monthly": 5900})
    c = make("C", match=1.0, base=1.0, factor=1.0, data={"salary_expect_floor_monthly": 6100})
    assert diversity.pair_similarity(a, b, "search_worker") == pytest.approx(1.0)
    assert diversity.pair_similarity(a, c, "search_worker") == pytest.approx(0.0)


def test_repeat_factor_buckets_are_left_closed_right_open():
    from app.services.recommendation_diversity_service import apply_repeat_factor
    from app.core.time_utils import utc_now

    now = utc_now()
    cooldown = 24
    cases = {"recent": 1, "middle": 9, "late": 17, "unseen": 25}
    for expected_bucket, hours in cases.items():
        candidate = make("X", match=1.0, base=1.0, factor=1.0)
        apply_repeat_factor(
            [candidate], recent_exposures={"X": float(hours)},
            cooldown_hours=cooldown, now=now,
        )
        assert candidate.repeat_bucket == expected_bucket, hours


def test_zero_cooldown_disables_repeat_damping():
    from app.services.recommendation_diversity_service import apply_repeat_factor

    candidate = make("X", match=1.0, base=1.0, factor=1.0)
    apply_repeat_factor([candidate], recent_exposures={"X": 0.5}, cooldown_hours=0)
    assert candidate.repeat_factor == 1.0
