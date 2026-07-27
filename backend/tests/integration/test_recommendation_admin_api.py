"""Admin DTO and permission boundary smoke tests."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.schemas.recommendation import (
    RecommendationStrategyParameters,
    RecommendationReleaseUpdate,
)
from app.services.admin_user_service import role_at_least

pytestmark = pytest.mark.integration


def test_admin_strategy_update_contract_rejects_invalid_weight_sum():
    with pytest.raises(ValueError):
        RecommendationStrategyParameters(
            match_weight=90,
            quality_weight=10,
            freshness_weight=10,
            exposure_weight=0,
        )


def test_admin_role_boundary_is_enforced_in_service_contract():
    assert role_at_least(SimpleNamespace(role="viewer"), "viewer")
    assert not role_at_least(SimpleNamespace(role="viewer"), "operator")
    assert role_at_least(SimpleNamespace(role="super_admin"), "operator")
    payload = RecommendationReleaseUpdate(
        execution_mode="off",
        candidate_version_id=None,
        rollout_percentage=0,
        lock_version=1,
        change_reason="integration",
    )
    assert payload.change_reason == "integration"
