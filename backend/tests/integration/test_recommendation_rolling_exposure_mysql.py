"""Rolling 168-hour exposure query uses UTC boundaries."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.time_utils import ensure_utc, to_naive_utc
from app.services.recommendation_metrics_service import resolve_window

pytestmark = pytest.mark.integration


def test_rolling_window_is_exactly_168_hours():
    now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    start, end = resolve_window(7, now)
    assert (end - start).total_seconds() == 7 * 24 * 3600
    assert to_naive_utc(ensure_utc(now)) == end
