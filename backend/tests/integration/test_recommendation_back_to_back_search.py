"""Recently sent-but-not-derived delivery facts participate in cooldown."""
from __future__ import annotations

import pytest

from app.services.recommendation_exposure_service import recent_user_exposures

pytestmark = pytest.mark.integration


def test_recent_exposure_query_is_available_against_real_database(db_session):
    values = recent_user_exposures(
        db_session,
        viewer_userid="integration-no-such-viewer",
        target_type="job",
        candidate_ids=["1", "2"],
        request_now_utc=None,
        cooldown_hours=24,
    )
    assert values == {}
