"""Release history is append-only and validates rollback targets."""
from __future__ import annotations

import pytest

from app.services.recommendation_strategy_service import (
    ReleaseStateError,
    append_release_history,
)

pytestmark = pytest.mark.integration


def test_release_history_requires_target_revision_for_rollback(db_session):
    with pytest.raises(ReleaseStateError):
        append_release_history(
            db_session,
            direction="search_job",
            revision=900001,
            operation="rollback",
            execution_mode="off",
            stable_version_id=None,
            candidate_version_id=None,
            rollout_percentage=0,
            change_reason="integration",
            created_by="integration",
        )
