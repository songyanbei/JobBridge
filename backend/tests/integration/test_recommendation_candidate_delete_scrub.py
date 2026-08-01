"""Target redaction helper is a no-op for an empty target set."""
from __future__ import annotations

import pytest

from app.services.recommendation_privacy_service import redact_deliveries_for_targets

pytestmark = pytest.mark.integration


def test_empty_target_scrub_is_idempotent(db_session):
    assert redact_deliveries_for_targets(
        db_session, [], commit=False,
    ) == set()
