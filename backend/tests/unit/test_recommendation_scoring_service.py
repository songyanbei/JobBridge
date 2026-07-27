"""§6.2-§6.7 scoring boundary tests."""
from __future__ import annotations

import pytest

from app.services.recommendation_scoring_service import (
    normalize_components,
    semantic_scores,
)


def test_component_normalization_distinguishes_zero_from_missing():
    assert normalize_components({
        "present_zero": (0.0, 0.6),
        "missing": (None, 0.4),
    }) == pytest.approx(0.0)
    assert normalize_components({
        "first": (None, 0.6),
        "second": (None, 0.4),
    }) == pytest.approx(0.5)


def test_semantic_ranks_ignore_unknown_duplicate_and_extra_ids():
    scores = semantic_scores(
        ["1", "2", "3", "4"],
        [
            {"id": "unknown"},
            {"id": "2"},
            {"id": "2"},
            {"id": "1"},
            {"id": "3"},
            {"id": "4"},
        ],
    )

    assert scores == {
        "1": pytest.approx(0.85),
        "2": pytest.approx(1.0),
        "3": pytest.approx(0.70),
        "4": pytest.approx(0.5),
    }


def test_failed_semantic_ranking_is_strictly_neutral():
    assert semantic_scores(["1", "2"], None, failed=True) == {
        "1": 0.5,
        "2": 0.5,
    }
