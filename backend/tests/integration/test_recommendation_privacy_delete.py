"""Empty-user deletion is idempotent and does not require candidate rows."""
from __future__ import annotations

import pytest

from app.services.recommendation_privacy_service import delete_recommendation_user_data

pytestmark = pytest.mark.integration


def test_delete_empty_user_is_idempotent(db_session):
    first = delete_recommendation_user_data(
        db_session, "integration-delete-no-such-user", commit=False,
    )
    second = delete_recommendation_user_data(
        db_session, "integration-delete-no-such-user", commit=False,
    )
    assert first.batch_id != second.batch_id
    assert first.failed_steps == []
    assert second.failed_steps == []
