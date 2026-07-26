"""§6.3.6 fixed match-score vectors, driven end to end through the real
candidate projections.

Going through `build_scored_candidate` rather than the bare `match_score`
primitive is deliberate: it is what catches a candidate dict that silently lost
a column, which is how the worker direction ended up scoring on almost nothing.
"""
import pytest

from app.services.recommendation_scoring_service import (
    build_scored_candidate,
    match_score,
    quality_score,
    salary_fit_score_detailed,
    semantic_scores,
)

TOLERANCE = 1e-4

# §6.3.2 rank → semantic score.
TOP1, TOP2, NOT_RETURNED = 1.0000, 0.8500, 0.5000

JOB_CRITERIA = {"salary_floor_monthly": 5000, "provide_meal": True}
# name, semantic, effective ceiling, expected salary, expected soft, expected match
JOB_VECTOR = [
    ("A", TOP1, 6000, 0.8333, 1.0000, 0.9500),
    ("B", TOP2, 5500, 0.6667, 1.0000, 0.8250),
    ("C", NOT_RETURNED, 5000, 0.5000, 1.0000, 0.6000),
]

WORKER_CRITERIA = {"salary_ceiling_monthly": 6000}
# name, semantic, expectation, expected salary, expected match
WORKER_VECTOR = [
    ("A", TOP1, 5000, 0.7778, 0.9167),
    ("B", TOP2, 6000, 0.5000, 0.7188),
    ("C", NOT_RETURNED, 5500, 0.6389, 0.5521),
]


@pytest.mark.parametrize("name,semantic,ceiling,exp_salary,exp_soft,exp_match", JOB_VECTOR)
def test_job_match_vector(name, semantic, ceiling, exp_salary, exp_soft, exp_match):
    scored = build_scored_candidate(
        {
            "id": name,
            "salary_floor_monthly": 5000,
            "salary_ceiling_monthly": ceiling,
            "provide_meal": True,
        },
        candidate_id=name,
        owner_userid="owner",
        direction="search_job",
        criteria=JOB_CRITERIA,
        semantic=semantic,
    )
    assert scored.salary_score == pytest.approx(exp_salary, abs=TOLERANCE)
    assert scored.soft_score == pytest.approx(exp_soft, abs=TOLERANCE)
    assert scored.match_score == pytest.approx(exp_match, abs=TOLERANCE)


@pytest.mark.parametrize("name,semantic,expectation,exp_salary,exp_match", WORKER_VECTOR)
def test_worker_match_vector(name, semantic, expectation, exp_salary, exp_match):
    scored = build_scored_candidate(
        {"id": name, "salary_expect_floor_monthly": expectation},
        candidate_id=name,
        owner_userid="owner",
        direction="search_worker",
        criteria=WORKER_CRITERIA,
        semantic=semantic,
    )
    assert scored.salary_score == pytest.approx(exp_salary, abs=TOLERANCE)
    # §6.3.4: candidate_search carries no structured worker preference fields,
    # so the soft component is always missing and the weights renormalize.
    assert scored.soft_score is None
    assert scored.match_score == pytest.approx(exp_match, abs=TOLERANCE)


def test_quality_line_partitions_the_job_vector():
    """0.95 best → 0.8075 quality line: A and B are near, C is ordinary."""
    best = 0.9500
    line = best * 0.85
    assert line == pytest.approx(0.8075, abs=TOLERANCE)
    assert 0.9500 >= line and 0.8250 >= line
    assert 0.6000 < line


def test_semantic_rank_conversion_and_cleaning_order():
    """§6.3.2: 1.00 / 0.85 / 0.70, unreturned 0.50, and the fixed cleaning
    order (unknown ids dropped, duplicates keep the first, cap at three)."""
    ids = ["1", "2", "3", "4", "5"]
    scores = semantic_scores(
        ids,
        [
            {"id": "3"},
            {"id": "999"},        # not in the input set - ignored
            {"id": "3"},          # duplicate - ignored
            {"score": 0.9},       # missing id - ignored
            {"id": "1"},
            {"id": "5"},
            {"id": "2"},          # beyond Top 3 - not scored
        ],
    )
    assert scores["3"] == TOP1
    assert scores["1"] == TOP2
    assert scores["5"] == pytest.approx(0.70)
    assert scores["2"] == NOT_RETURNED
    assert scores["4"] == NOT_RETURNED


def test_llm_failure_flattens_every_candidate():
    ids = ["1", "2", "3"]
    assert semantic_scores(ids, None, failed=True) == {i: NOT_RETURNED for i in ids}
    assert semantic_scores(ids, [], failed=False) == {i: NOT_RETURNED for i in ids}


def test_hard_filter_contract_violation_is_flagged_for_removal():
    """§6.3.3: a candidate whose pay cannot meet the query scores 0 *and* has to
    leave the result set rather than merely rank low."""
    score, broken = salary_fit_score_detailed(
        "search_job", {"salary_floor_monthly": 5000},
        {"salary_ceiling_monthly": 4000, "salary_floor_monthly": 4000},
    )
    assert score == 0.0 and broken is True

    scored = build_scored_candidate(
        {"id": "X", "salary_floor_monthly": 4000, "salary_ceiling_monthly": 4000},
        candidate_id="X", owner_userid="o", direction="search_job",
        criteria={"salary_floor_monthly": 5000}, semantic=TOP1,
    )
    assert scored.hard_filter_contract_broken is True
    assert "hard_filter_contract_broken" in scored.reason_codes


def test_quality_score_counts_false_as_a_present_value():
    """§6.4: bool False is an explicit answer, not a missing field."""
    present = quality_score({"provide_meal": False, "provide_housing": False}, "search_job")
    absent = quality_score({"provide_meal": None, "provide_housing": None}, "search_job")
    assert present > absent


def test_worker_quality_score_uses_all_nine_fields():
    """Six of these columns were missing from the resume projection, which
    capped worker quality_score at 3/9."""
    full = {
        "description": "x", "education": "高中", "work_experience": "3 年",
        "expected_districts": ["A"], "available_from": "2026-01-01",
        "accept_night_shift": True, "accept_overtime": False,
        "accept_long_term": True, "accept_short_term": False,
    }
    assert quality_score(full, "search_worker") == pytest.approx(1.0)
    partial = {"description": "x", "education": "高中", "work_experience": "3 年"}
    assert quality_score(partial, "search_worker") == pytest.approx(3 / 9, abs=TOLERANCE)


def test_match_all_components_missing_defaults_and_is_flagged():
    from app.services.recommendation_scoring_service import match_score_detailed

    value, all_missing = match_score_detailed(semantic=None, salary=None, soft=None)
    assert value == pytest.approx(0.5) and all_missing is True
    assert match_score(semantic=TOP1, salary=None, soft=None) == pytest.approx(TOP1)
