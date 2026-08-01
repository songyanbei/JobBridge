"""Stable user bucketing and rollout boundaries."""
from __future__ import annotations

import pytest

from app.services.recommendation_scoring_service import bucket_hit

pytestmark = pytest.mark.integration


def test_rollout_zero_and_full_are_hard_boundaries():
    assert bucket_hit(0, "integration-user", "search_job", 900001) is False
    assert bucket_hit(100, "integration-user", "search_job", 900001) is True
