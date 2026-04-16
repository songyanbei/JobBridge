"""企微客户端测试：send_text、download_media、get_external_contact。

所有外部 HTTP 调用通过 mock 完成。
"""
import json
import pytest
from unittest.mock import patch, MagicMock

import httpx

from app.wecom.client import WeComClient, WeComError


@pytest.fixture
def client():
    return WeComClient(corp_id="test_corp", secret="test_secret", agent_id="1000001")


class TestClientInit:

    def test_invalid_agent_id_raises(self):
        with pytest.raises(ValueError, match="numeric string"):
            WeComClient(corp_id="c", secret="s", agent_id="abc")

    def test_empty_agent_id_defaults_to_zero(self):
        c = WeComClient(corp_id="c", secret="s", agent_id="")
        assert c._agent_id == 0

    def test_valid_agent_id_converted_to_int(self):
        c = WeComClient(corp_id="c", secret="s", agent_id="1000001")
        assert c._agent_id == 1000001


class TestInvalidateToken:
    """P1-3：公开的 invalidate_token() 把缓存 token 清零，持锁写入。"""

    def test_invalidate_clears_cached_token(self, client):
        client._access_token = "some_token"
        client._token_expires_at = 9999999999
        client.invalidate_token()
        assert client._access_token == ""
        assert client._token_expires_at == 0

    def test_invalidate_uses_lock(self, client):
        """持锁写入，验证 _lock 被 acquire 过。"""
        original_lock = client._lock
        acquire_count = {"n": 0}

        class _LockSpy:
            def __enter__(self):
                acquire_count["n"] += 1
                return original_lock.__enter__()

            def __exit__(self, *args):
                return original_lock.__exit__(*args)

        client._lock = _LockSpy()
        client.invalidate_token()
        assert acquire_count["n"] == 1


@pytest.fixture
def authed_client(client):
    """已有有效 token 的客户端。"""
    client._access_token = "valid_token"
    client._token_expires_at = 9999999999
    return client


# ---------------------------------------------------------------------------
# Access Token
# ---------------------------------------------------------------------------

class TestAccessToken:

    @patch("app.wecom.client.httpx.get")
    def test_refresh_token_success(self, mock_get, client):
        mock_get.return_value = httpx.Response(
            200,
            json={"errcode": 0, "access_token": "new_token", "expires_in": 7200},
            request=httpx.Request("GET", "https://example.com"),
        )
        token = client.get_access_token()
        assert token == "new_token"

    @patch("app.wecom.client.httpx.get")
    def test_refresh_token_failure_raises(self, mock_get, client):
        mock_get.return_value = httpx.Response(
            200,
            json={"errcode": 40013, "errmsg": "invalid corpid"},
            request=httpx.Request("GET", "https://example.com"),
        )
        with pytest.raises(WeComError, match="invalid corpid"):
            client.get_access_token()

    def test_cached_token_not_expired(self, authed_client):
        """token 未过期时不重新获取。"""
        token = authed_client.get_access_token()
        assert token == "valid_token"


# ---------------------------------------------------------------------------
# send_text
# ---------------------------------------------------------------------------

class TestSendText:

    @patch("app.wecom.client.httpx.post")
    def test_send_text_success(self, mock_post, authed_client):
        mock_post.return_value = httpx.Response(
            200,
            json={"errcode": 0, "errmsg": "ok"},
            request=httpx.Request("POST", "https://example.com"),
        )
        result = authed_client.send_text("user001", "Hello!")
        assert result["errcode"] == 0

        # 验证请求 payload
        call_kwargs = mock_post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert payload["touser"] == "user001"
        assert payload["text"]["content"] == "Hello!"
        assert payload["msgtype"] == "text"

    @patch("app.wecom.client.httpx.post")
    def test_send_text_failure_raises(self, mock_post, authed_client):
        mock_post.return_value = httpx.Response(
            200,
            json={"errcode": 40014, "errmsg": "invalid access_token"},
            request=httpx.Request("POST", "https://example.com"),
        )
        with pytest.raises(WeComError):
            authed_client.send_text("user001", "Hello!")


# ---------------------------------------------------------------------------
# download_media
# ---------------------------------------------------------------------------

class TestDownloadMedia:

    @patch("app.wecom.client.httpx.get")
    def test_download_success(self, mock_get, authed_client):
        mock_resp = httpx.Response(
            200,
            content=b"image_binary_data",
            headers={"content-type": "image/jpeg"},
            request=httpx.Request("GET", "https://example.com"),
        )
        mock_get.return_value = mock_resp

        data = authed_client.download_media("media_123")
        assert data == b"image_binary_data"

    @patch("app.wecom.client.httpx.get")
    def test_download_error_response_raises(self, mock_get, authed_client):
        mock_get.return_value = httpx.Response(
            200,
            json={"errcode": 40007, "errmsg": "invalid media_id"},
            headers={"content-type": "application/json"},
            request=httpx.Request("GET", "https://example.com"),
        )
        with pytest.raises(WeComError, match="invalid media_id"):
            authed_client.download_media("bad_media")


# ---------------------------------------------------------------------------
# get_external_contact
# ---------------------------------------------------------------------------

class TestGetExternalContact:

    @patch("app.wecom.client.httpx.get")
    def test_contact_exists(self, mock_get, authed_client):
        mock_get.return_value = httpx.Response(
            200,
            json={
                "errcode": 0,
                "external_contact": {
                    "external_userid": "ext_user_001",
                    "name": "张三",
                    "type": 1,
                },
            },
            request=httpx.Request("GET", "https://example.com"),
        )
        contact = authed_client.get_external_contact("ext_user_001")
        assert contact is not None
        assert contact["name"] == "张三"

    @patch("app.wecom.client.httpx.get")
    def test_contact_not_found_returns_none(self, mock_get, authed_client):
        """用户不存在返回 None，不抛异常。"""
        mock_get.return_value = httpx.Response(
            200,
            json={"errcode": 84061, "errmsg": "not external userid"},
            request=httpx.Request("GET", "https://example.com"),
        )
        result = authed_client.get_external_contact("nonexistent")
        assert result is None

    @patch("app.wecom.client.httpx.get")
    def test_api_error_raises(self, mock_get, authed_client):
        """非"用户不存在"的错误必须抛异常，不返回 None。"""
        mock_get.return_value = httpx.Response(
            200,
            json={"errcode": 40014, "errmsg": "invalid access_token"},
            request=httpx.Request("GET", "https://example.com"),
        )
        with pytest.raises(WeComError):
            authed_client.get_external_contact("ext_user_001")

    @patch("app.wecom.client.httpx.get")
    def test_network_error_raises(self, mock_get, authed_client):
        """网络错误必须抛异常，不返回 None。"""
        mock_get.side_effect = httpx.ConnectError("connection refused")
        with pytest.raises(httpx.ConnectError):
            authed_client.get_external_contact("ext_user_001")

    def test_none_vs_exception_semantics(self):
        """语义验证: None 只表示用户不存在，其他情况必须异常。"""
        # 这是一个文档性测试，验证 client 代码中的语义设计
        from app.wecom.client import WeComClient
        import inspect
        src = inspect.getsource(WeComClient.get_external_contact)
        # 确保有 None return 和 raise 的分支
        assert "return None" in src
        assert "raise" in src
