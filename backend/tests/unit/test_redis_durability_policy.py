import asyncio
from unittest.mock import MagicMock

import pytest

from app.core.redis_client import (
    RedisDurabilityPolicyError,
    validate_redis_durability_policy,
)


class _ConfiguredRedis:
    def __init__(self, values):
        self.values = values

    def config_get(self, name):
        value = self.values.get(name)
        return {} if value is None else {name: value}


def test_validate_redis_durability_policy_accepts_required_settings():
    configured = {
        "maxmemory-policy": "noeviction",
        "appendonly": "yes",
        "appendfsync": "always",
    }

    assert validate_redis_durability_policy(_ConfiguredRedis(configured)) == configured


@pytest.mark.parametrize(
    ("name", "unsafe_value", "expected_fragment"),
    [
        ("maxmemory-policy", "allkeys-lru", "expected noeviction"),
        ("appendonly", "no", "expected yes"),
        ("appendfsync", "everysec", "expected always"),
        ("appendonly", None, "appendonly=<missing>"),
    ],
)
def test_validate_redis_durability_policy_rejects_unsafe_settings(
    name,
    unsafe_value,
    expected_fragment,
):
    configured = {
        "maxmemory-policy": "noeviction",
        "appendonly": "yes",
        "appendfsync": "always",
    }
    configured[name] = unsafe_value

    with pytest.raises(RedisDurabilityPolicyError) as exc_info:
        validate_redis_durability_policy(_ConfiguredRedis(configured))

    assert f"{name}=" in str(exc_info.value)
    assert expected_fragment in str(exc_info.value)


def test_api_startup_rejects_unsafe_redis_before_background_services(monkeypatch):
    from app import main as app_main

    scheduler_start = MagicMock()
    monkeypatch.setattr(app_main.task_scheduler, "start", scheduler_start)
    monkeypatch.setattr(
        app_main,
        "validate_redis_durability_policy",
        MagicMock(side_effect=RedisDurabilityPolicyError("unsafe")),
    )

    async def _start_app():
        async with app_main.lifespan(app_main.app):
            pass

    with pytest.raises(RedisDurabilityPolicyError, match="unsafe"):
        asyncio.run(_start_app())

    scheduler_start.assert_not_called()


def test_worker_start_rejects_unsafe_redis_before_background_threads(monkeypatch):
    from app.services import worker as worker_module

    redis_client = MagicMock()
    monkeypatch.setattr(worker_module, "get_redis", lambda: redis_client)
    monkeypatch.setattr(worker_module, "WeComClient", MagicMock())
    monkeypatch.setattr(
        worker_module,
        "validate_redis_durability_policy",
        MagicMock(side_effect=RedisDurabilityPolicyError("unsafe")),
    )
    worker = worker_module.Worker()

    with pytest.raises(RedisDurabilityPolicyError, match="unsafe"):
        worker.start()

    assert worker._heartbeat_thread is None
    assert worker._aux_threads == []
