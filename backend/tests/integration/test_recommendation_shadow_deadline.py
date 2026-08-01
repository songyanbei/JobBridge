"""Shadow absolute deadline contract on a real async event loop."""
from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import pytest

from app.services.recommendation_shadow_service import ShadowPolicy, ShadowRunner
from .test_recommendation_shadow_mode import _job

pytestmark = pytest.mark.integration


def test_expired_shadow_job_never_calls_provider():
    calls = []

    class Provider:
        async def arerank(self, **kwargs):
            calls.append(kwargs)
            raise AssertionError("expired job must not call provider")

    runner = ShadowRunner(
        ShadowPolicy(),
        reranker_factory=Provider,
        redis_factory=lambda: None,
    )
    try:
        result = asyncio.run(runner._execute(replace(
            _job("deadline-integration"),
            deadline_monotonic=time.monotonic() - 1,
        )))
    finally:
        runner.shutdown()
    assert result.status == "timeout_in_queue"
    assert not calls
