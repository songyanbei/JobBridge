"""Tests for the [MOCK-WEWORK] env-gated branches in
backend/app/wecom/client.py.

These tests verify that:
1. When MOCK_WEWORK_OUTBOUND is unset / false, send_text / send_text_to_group
   fall through to the real httpx path (unchanged behaviour).
2. When MOCK_WEWORK_OUTBOUND=true, both methods short-circuit to publish
   the canonical WeCom payload shape onto a Redis channel and return the
   original return type (dict vs bool) without touching httpx.

The test file lives in the main backend test tree (not in mock-testbed/)
because the seam it validates lives in main backend/app/wecom/client.py.
It imports no mock-testbed code, so the mock-testbed/ dir can be removed
wholesale without breaking this test — it will simply keep asserting the
"fall-through" behaviour once the [MOCK-WEWORK] block is also removed.

NOTE: when the [MOCK-WEWORK] block is removed from client.py (real WeCom
integration day), this file should be deleted too — see
mock-testbed/README.md §删除指南 step 4.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from app.wecom.client import WeComClient


@pytest.fixture
def authed_client():
    """A WeComClient instance with a pre-cached access_token so we never
    accidentally call /cgi-bin/gettoken in fall-through tests."""
    c = WeComClient(corp_id="test_corp", secret="test_secret", agent_id="1000001")
    c._access_token = "cached-token"  # type: ignore[attr-defined]
    import time as _time
    c._token_expires_at = _time.time() + 3600  # type: ignore[attr-defined]
    return c


# ----------------------------------------------------------------------------
# Fall-through: flag unset / false → real path
# ----------------------------------------------------------------------------

class TestFallThroughWhenFlagDisabled:
    def test_send_text_uses_real_path_when_flag_unset(self, authed_client, monkeypatch):
        monkeypatch.delenv("MOCK_WEWORK_OUTBOUND", raising=False)
        with patch("app.wecom.client.httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"errcode": 0, "errmsg": "ok"}
            mock_post.return_value = mock_resp

            result = authed_client.send_text("user001", "hello")

            assert mock_post.call_count == 1
            assert result == {"errcode": 0, "errmsg": "ok"}

    def test_send_text_uses_real_path_when_flag_false(self, authed_client, monkeypatch):
        monkeypatch.setenv("MOCK_WEWORK_OUTBOUND", "false")
        with patch("app.wecom.client.httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"errcode": 0, "errmsg": "ok"}
            mock_post.return_value = mock_resp

            authed_client.send_text("user001", "hello")
            assert mock_post.call_count == 1

    def test_send_text_to_group_uses_real_path_when_flag_unset(self, authed_client, monkeypatch):
        monkeypatch.delenv("MOCK_WEWORK_OUTBOUND", raising=False)
        with patch("app.wecom.client.httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"errcode": 0, "errmsg": "ok"}
            mock_post.return_value = mock_resp

            ok = authed_client.send_text_to_group("chat_id_x", "group content")
            assert ok is True
            assert mock_post.call_count == 1


# ----------------------------------------------------------------------------
# Short-circuit: flag=true → publish to Redis, no httpx
# ----------------------------------------------------------------------------

class _FakeRedis:
    """Minimal Redis-double capturing publish calls; no real I/O."""

    def __init__(self):
        self.published: list[tuple[str, str]] = []

    def publish(self, channel: str, data: str) -> int:
        self.published.append((channel, data))
        return 1


class TestShortCircuitWhenFlagEnabled:
    def _install_fake_redis(self, monkeypatch):
        fake = _FakeRedis()

        class _FakeRedisMod:
            Redis = MagicMock()
            Redis.from_url = staticmethod(lambda url, **kwargs: fake)

        monkeypatch.setitem(__import__("sys").modules, "redis", _FakeRedisMod)
        return fake

    def test_send_text_short_circuits_and_publishes(self, authed_client, monkeypatch):
        monkeypatch.setenv("MOCK_WEWORK_OUTBOUND", "true")
        fake = self._install_fake_redis(monkeypatch)

        with patch("app.wecom.client.httpx.post") as mock_post:
            result = authed_client.send_text("wm_mock_worker_001", "hi")
            # httpx must NOT be called
            assert mock_post.call_count == 0

        # Real path was short-circuited; return type is dict matching original signature
        assert isinstance(result, dict)
        assert result["errcode"] == 0
        assert result["errmsg"] == "ok"
        assert result["msgid"].startswith("mock_")

        # Exactly one publish happened, to correct channel
        assert len(fake.published) == 1
        channel, data = fake.published[0]
        assert channel == "mock:outbound:wm_mock_worker_001"

        # Payload shape strictly matches WeCom /cgi-bin/message/send request body
        payload = json.loads(data)
        assert set(payload.keys()) == {"touser", "msgtype", "agentid", "text"}
        assert payload["touser"] == "wm_mock_worker_001"
        assert payload["msgtype"] == "text"
        assert payload["text"] == {"content": "hi"}

    def test_send_text_to_group_short_circuits_and_publishes(self, authed_client, monkeypatch):
        monkeypatch.setenv("MOCK_WEWORK_OUTBOUND", "true")
        fake = self._install_fake_redis(monkeypatch)

        with patch("app.wecom.client.httpx.post") as mock_post:
            ok = authed_client.send_text_to_group("gid_test_123", "group hello")
            # httpx NOT called
            assert mock_post.call_count == 0

        # Return type is bool matching original signature
        assert ok is True

        assert len(fake.published) == 1
        channel, data = fake.published[0]
        assert channel == "mock:outbound:chat:gid_test_123"

        payload = json.loads(data)
        assert set(payload.keys()) == {"chatid", "msgtype", "text", "safe"}
        assert payload["chatid"] == "gid_test_123"
        assert payload["msgtype"] == "text"
        assert payload["text"] == {"content": "group hello"}
        assert payload["safe"] == 0

    def test_empty_chat_id_still_short_circuits_to_false(self, authed_client, monkeypatch):
        """For send_text_to_group, the `if not chat_id` early-return runs
        BEFORE the [MOCK-WEWORK] block — so empty chat_id always returns
        False regardless of flag. Verifies the block is correctly placed.
        """
        monkeypatch.setenv("MOCK_WEWORK_OUTBOUND", "true")
        fake = self._install_fake_redis(monkeypatch)

        ok = authed_client.send_text_to_group("", "anything")
        assert ok is False
        assert len(fake.published) == 0

    def test_redis_exception_swallowed_not_raised(self, authed_client, monkeypatch):
        """If Redis blows up the mock branch logs + still returns success —
        we don't want a Redis blip to break Worker message delivery."""
        monkeypatch.setenv("MOCK_WEWORK_OUTBOUND", "true")

        class _BrokenRedisMod:
            Redis = MagicMock()
            Redis.from_url = staticmethod(lambda url, **kwargs: _raises())

        def _raises():
            raise RuntimeError("redis down")

        monkeypatch.setitem(__import__("sys").modules, "redis", _BrokenRedisMod)

        # Should not raise; still returns mock-shaped dict
        result = authed_client.send_text("wm_mock_worker_001", "hi")
        assert result["errcode"] == 0

    def test_return_dict_matches_wecom_success_shape(self, authed_client, monkeypatch):
        """Mock send_text return body aligns with /cgi-bin/message/send success
        response (partial-failure fields present even when empty)."""
        monkeypatch.setenv("MOCK_WEWORK_OUTBOUND", "true")
        self._install_fake_redis(monkeypatch)
        result = authed_client.send_text("wm_mock_worker_001", "hi")
        assert result["errcode"] == 0
        assert result["errmsg"] == "ok"
        assert result["msgid"].startswith("mock_")
        # Partial-failure fields must exist (even empty) so downstream
        # consumers can rely on WeCom's official success-response shape
        for k in ("invaliduser", "invalidparty", "invalidtag",
                  "unlicenseduser", "response_code"):
            assert k in result, f"missing WeCom field: {k}"


# ----------------------------------------------------------------------------
# Production environment guard — refuses to activate even if env var leaks
# ----------------------------------------------------------------------------

class TestProductionGuard:
    def _install_fake_redis(self, monkeypatch):
        fake = _FakeRedis()

        class _FakeRedisMod:
            Redis = MagicMock()
            Redis.from_url = staticmethod(lambda url, **kwargs: fake)

        monkeypatch.setitem(__import__("sys").modules, "redis", _FakeRedisMod)
        return fake

    def test_send_text_raises_in_production(self, authed_client, monkeypatch):
        """If APP_ENV=production + MOCK_WEWORK_OUTBOUND=true both set
        (operator mistake), the mock branch MUST raise rather than silently
        short-circuit real users' outbound messages."""
        monkeypatch.setenv("MOCK_WEWORK_OUTBOUND", "true")
        monkeypatch.setenv("APP_ENV", "production")
        self._install_fake_redis(monkeypatch)
        with pytest.raises(RuntimeError, match=r"production"):
            authed_client.send_text("wm_mock_worker_001", "hi")

    def test_send_text_to_group_raises_in_production(self, authed_client, monkeypatch):
        monkeypatch.setenv("MOCK_WEWORK_OUTBOUND", "true")
        monkeypatch.setenv("APP_ENV", "production")
        self._install_fake_redis(monkeypatch)
        with pytest.raises(RuntimeError, match=r"production"):
            authed_client.send_text_to_group("chat_x", "hi")

    def test_non_production_env_allows_mock(self, authed_client, monkeypatch):
        """APP_ENV=development / staging / anything non-production → mock works."""
        monkeypatch.setenv("MOCK_WEWORK_OUTBOUND", "true")
        monkeypatch.setenv("APP_ENV", "development")
        self._install_fake_redis(monkeypatch)
        result = authed_client.send_text("wm_mock_worker_001", "hi")
        assert result["errcode"] == 0
