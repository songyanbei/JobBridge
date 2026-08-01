"""Two runner instances share one Redis global permit."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
import redis.asyncio as redis

from app.config import settings
from app.services.recommendation_shadow_service import ShadowPolicy, ShadowRunner

pytestmark = pytest.mark.integration


def test_global_permit_is_deployment_wide_not_process_local():
    async def run():
        client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
        first = ShadowRunner(ShadowPolicy(global_concurrency=1), redis_factory=lambda: client)
        second = ShadowRunner(ShadowPolicy(global_concurrency=1), redis_factory=lambda: client)
        job = type("Job", (), {
            "provider": "integration-multiworker",
            "direction": "search_job",
            "scoring_time_utc": datetime.now(timezone.utc),
        })()
        try:
            token, key = await first._acquire_global_permit(client, job)
            denied, reason = await second._acquire_global_permit(client, job)
            assert token is not None
            assert denied is None
            assert reason == "global_capacity"
            await first._release_global_permit(client, key, token)
        finally:
            await client.aclose()
            first.shutdown()
            second.shutdown()
    asyncio.run(run())
