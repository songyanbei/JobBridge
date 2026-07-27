"""Published strategy version cache is immutable-keyed and time bounded."""
from __future__ import annotations

import pytest

from app.core import redis_client

pytestmark = pytest.mark.integration


def test_strategy_cache_round_trip_uses_version_id_key():
    version_id = "integration-cache-900001"
    payload = {"id": 900001, "algorithm_version": "recommendation-v1"}
    try:
        redis_client.set_cached_strategy_version(version_id, payload, ttl=30)
        assert redis_client.get_cached_strategy_version(version_id) == payload
    finally:
        redis_client.get_redis().delete(
            f"{redis_client.STRATEGY_VERSION_CACHE_PREFIX}{version_id}",
        )
