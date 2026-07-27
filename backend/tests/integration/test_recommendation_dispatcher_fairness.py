"""Dispatcher scheduling is independent from the incoming queue."""
from __future__ import annotations

import pytest

from app.tasks import recommendation_delivery_dispatcher, recommendation_impression_deriver

pytestmark = pytest.mark.integration


def test_recovery_scanners_have_bounded_batches_and_fixed_intervals():
    assert recommendation_delivery_dispatcher.BATCH_SIZE == 100
    assert recommendation_impression_deriver.BATCH_SIZE == 100
    assert recommendation_delivery_dispatcher.SCAN_INTERVAL_SECONDS <= 0.25
    assert recommendation_impression_deriver.SCAN_INTERVAL_SECONDS <= 0.25
