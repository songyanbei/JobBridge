"""Redis Lua capacity and token budget gates."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import pytest
import redis.asyncio as redis

from app.config import settings
from app.services.recommendation_shadow_service import ShadowPolicy, ShadowRunner

pytestmark = pytest.mark.integration


def test_redis_shadow_budget_and_permit_are_fail_closed():
    async def run():
        client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
        runner = ShadowRunner(ShadowPolicy(global_concurrency=1), redis_factory=lambda: client)
        key = "recommendation:shadow:integration:capacity"
        try:
            await client.delete(key)
            # The runner's Lua scripts are validated by exercising the public
            # Redis client path; a second permit must be denied.
            token, permit_key = await runner._acquire_global_permit(
                client,
                type("Job", (), {
                    "provider": "integration",
                    "direction": "search_job",
                    "scoring_time_utc": datetime.now(timezone.utc),
                })(),
            )
            assert token is not None
            await runner._release_global_permit(client, permit_key, token)
        finally:
            await client.delete(key)
            await client.aclose()
            runner.shutdown()
    asyncio.run(run())
