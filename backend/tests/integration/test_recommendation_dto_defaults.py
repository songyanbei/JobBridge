"""Public DTO defaults remain safe for old callers."""
from __future__ import annotations

import pytest

from app.schemas.report import AttemptMetrics, ShadowMetrics
from app.schemas.search import SearchResult

pytestmark = pytest.mark.integration


def test_new_metrics_and_search_fields_are_backward_compatible():
    assert AttemptMetrics().ranking_attempts == 0
    assert ShadowMetrics().persistence_drop_count is None
    assert SearchResult(reply_text="ok").llm_status == "skipped"
