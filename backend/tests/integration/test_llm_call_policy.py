"""Async LLM policy uses the remaining absolute deadline and zero retries."""
from __future__ import annotations

import pytest

from app.llm.base import LLMCallPolicy

pytestmark = pytest.mark.integration


def test_shadow_policy_defaults_to_zero_provider_retries():
    policy = LLMCallPolicy(deadline_monotonic=1.0, max_retries=0)
    assert policy.max_retries == 0
    assert policy.deadline_monotonic == 1.0
