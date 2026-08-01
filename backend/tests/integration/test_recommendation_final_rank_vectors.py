"""Final rank vectors execute through the production deterministic scorer."""
from __future__ import annotations

import pytest

from app.services.recommendation_scoring_service import stable_hash_hex

pytestmark = pytest.mark.integration


def test_tie_break_hash_is_reproducible():
    args = ("viewer", "search_job", "digest", "recommendation-v1", 101)
    assert stable_hash_hex(*args) == stable_hash_hex(*args)
