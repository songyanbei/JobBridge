"""Legacy generic configuration remains isolated from v1 constants."""
from __future__ import annotations

import pytest

from app.services.recommendation_scoring_service import V1_DISPLAY_TOP_N, V1_MAX_CANDIDATES

pytestmark = pytest.mark.integration


def test_v1_contract_constants_are_stable():
    assert V1_DISPLAY_TOP_N == 3
    assert V1_MAX_CANDIDATES == 50
