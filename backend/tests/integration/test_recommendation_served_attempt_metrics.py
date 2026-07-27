"""Served attempt and shadow attempts remain separate in report aggregation."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services.recommendation_metrics_service import percentile

pytestmark = pytest.mark.integration


def test_report_percentile_is_linear_and_empty_is_unknown():
    assert percentile([0, 10, 20, 30], 0.95) == 28.5
    assert percentile([], 0.95) is None
