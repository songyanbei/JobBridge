"""Business timezone and UTC storage boundaries."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.time_utils import business_date, rotation_date

pytestmark = pytest.mark.integration


def test_business_day_rolls_at_beijing_midnight():
    before = datetime(2026, 7, 26, 15, 59, 59, tzinfo=timezone.utc)
    after = datetime(2026, 7, 26, 16, 0, 0, tzinfo=timezone.utc)
    assert business_date(before).isoformat() == "2026-07-26"
    assert business_date(after).isoformat() == "2026-07-27"
    assert rotation_date(after) == "2026-07-27"
