"""Per-user Redis lock renewal tests."""
import time
from unittest.mock import MagicMock, patch

import pytest

from app.core import redis_client


def test_user_lock_renews_while_context_is_active(monkeypatch):
    monkeypatch.setattr(redis_client, "LOCK_RENEW_INTERVAL_SECONDS", 0.01)
    fake_lock = MagicMock()
    fake_lock.acquire.return_value = True
    fake_lock.extend.return_value = True
    fake_redis = MagicMock()
    fake_redis.lock.return_value = fake_lock

    with patch.object(redis_client, "get_redis", return_value=fake_redis):
        with redis_client.user_lock("u1", timeout=2) as acquired:
            assert bool(acquired) is True
            time.sleep(0.035)
            acquired.assert_owned()

    assert fake_lock.extend.call_count >= 1
    fake_lock.extend.assert_called_with(
        redis_client.LOCK_TTL, replace_ttl=True,
    )
    fake_lock.release.assert_called_once()
    fake_lock.owned.assert_called_once()
    assert fake_redis.lock.call_args.kwargs["thread_local"] is False


def test_user_lock_does_not_start_renewal_when_not_acquired(monkeypatch):
    monkeypatch.setattr(redis_client, "LOCK_RENEW_INTERVAL_SECONDS", 0.01)
    fake_lock = MagicMock()
    fake_lock.acquire.return_value = False
    fake_redis = MagicMock()
    fake_redis.lock.return_value = fake_lock

    with patch.object(redis_client, "get_redis", return_value=fake_redis):
        with redis_client.user_lock("u1", timeout=0) as acquired:
            assert bool(acquired) is False
            time.sleep(0.02)

    fake_lock.extend.assert_not_called()
    fake_lock.release.assert_not_called()


def test_lease_rejects_commit_after_renewal_loss():
    lost = redis_client.threading.Event()
    lock = MagicMock()
    lease = redis_client.UserLockLease(True, lock, lost, "abc")
    lost.set()

    with pytest.raises(redis_client.UserLockLost):
        lease.assert_owned()
    lock.owned.assert_not_called()


def test_user_lock_acquire_redis_error_degrades_to_not_acquired():
    fake_lock = MagicMock()
    fake_lock.acquire.side_effect = redis_client.redis.exceptions.ConnectionError("down")
    fake_redis = MagicMock()
    fake_redis.lock.return_value = fake_lock

    with patch.object(redis_client, "get_redis", return_value=fake_redis):
        with redis_client.user_lock("u1", timeout=0) as lease:
            assert not lease


def test_user_lock_release_redis_error_does_not_escape():
    fake_lock = MagicMock()
    fake_lock.acquire.return_value = True
    fake_lock.release.side_effect = redis_client.redis.exceptions.ConnectionError("down")
    fake_redis = MagicMock()
    fake_redis.lock.return_value = fake_lock

    with patch.object(redis_client, "get_redis", return_value=fake_redis), patch.object(
        redis_client, "LOCK_RENEW_INTERVAL_SECONDS", 60,
    ):
        with redis_client.user_lock("u1", timeout=0) as lease:
            assert lease
