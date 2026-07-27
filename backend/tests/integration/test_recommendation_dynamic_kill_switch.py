"""Runtime control accepts monotonic revisions and rejects stale updates."""
from __future__ import annotations

import pytest

from app.services import recommendation_strategy_service as service

pytestmark = pytest.mark.integration


def test_runtime_control_revision_is_monotonic():
    service.reset_runtime_control_cache()
    service.apply_runtime_control_update(
        {"kill_switch": True, "revision": 2000},
        source="integration",
    )
    current = service.apply_runtime_control_update(
        {"kill_switch": False, "revision": 1999},
        source="stale",
    )
    assert current.kill_switch is True
    assert current.revision == 2000
    service.reset_runtime_control_cache()
