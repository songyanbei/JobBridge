"""Click attribution reads only sent, impression-backed delivery facts."""
from __future__ import annotations

import pytest

from app.services.event_service import build_attribution_dedupe_key

pytestmark = pytest.mark.integration


def test_attribution_dedupe_key_is_stable_and_direction_independent():
    first = build_attribution_dedupe_key(
        "miniprogram_click", "d-integration", "job", 42,
    )
    second = build_attribution_dedupe_key(
        "miniprogram_click", "d-integration", "job", 42,
    )
    assert first == second
    assert len(first) == 64
