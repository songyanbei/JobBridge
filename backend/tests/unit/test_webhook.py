"""webhook 端点单元测试（Phase 4）。

使用 FastAPI TestClient 驱动，对 crypto / DB / Redis 全部 mock。
重点验证：
- 验签成功/失败
- 幂等短路不入队
- 被限流不写 inbound_event 不入队
- Happy path 写 inbound_event + 入队
- 解密失败返回 200（避免企微重试）
- 端到端响应时间（这里只做接口行为，性能在集成测试里）
"""
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.webhook import router as webhook_router


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(webhook_router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /webhook/wecom —— URL 验证
# ---------------------------------------------------------------------------

class TestVerifyCallback:
    @patch("app.api.webhook.decrypt_message", return_value="hello")
    @patch("app.api.webhook.verify_signature", return_value=True)
    def test_get_valid_signature_returns_echostr(self, mock_verify, mock_decrypt, client):
        resp = client.get(
            "/webhook/wecom",
            params={"msg_signature": "s", "timestamp": "t", "nonce": "n", "echostr": "e"},
        )
        assert resp.status_code == 200
        assert resp.text == "hello"
        mock_verify.assert_called_once()

    @patch("app.api.webhook.verify_signature", return_value=False)
    def test_get_bad_signature_returns_403(self, mock_verify, client):
        resp = client.get(
            "/webhook/wecom",
            params={"msg_signature": "bad", "timestamp": "t", "nonce": "n", "echostr": "e"},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /webhook/wecom —— 消息接收
# ---------------------------------------------------------------------------

_FAKE_XML = """<xml><Encrypt>some_encrypt</Encrypt></xml>"""


def _fake_plaintext(content="你好", msg_id="m1", from_user="u1", msg_type="text"):
    return f"""<xml>
<ToUserName>bot</ToUserName>
<FromUserName>{from_user}</FromUserName>
<CreateTime>1700000000</CreateTime>
<MsgType>{msg_type}</MsgType>
<Content>{content}</Content>
<MsgId>{msg_id}</MsgId>
</xml>"""


class TestReceiveCallback:
    @patch("app.api.webhook.verify_signature", return_value=False)
    def test_post_bad_signature_returns_403(self, mock_verify, client):
        resp = client.post(
            "/webhook/wecom",
            params={"msg_signature": "bad", "timestamp": "t", "nonce": "n"},
            content=_FAKE_XML,
        )
        assert resp.status_code == 403

    @patch("app.api.webhook.decrypt_message", side_effect=ValueError("decrypt fail"))
    @patch("app.api.webhook.verify_signature", return_value=True)
    def test_post_decrypt_failure_returns_200(
        self, mock_verify, mock_decrypt, client,
    ):
        resp = client.post(
            "/webhook/wecom",
            params={"msg_signature": "s", "timestamp": "t", "nonce": "n"},
            content=_FAKE_XML,
        )
        assert resp.status_code == 200
        assert resp.text == "success"

    @patch("app.api.webhook.enqueue_message")
    @patch("app.api.webhook._insert_inbound_event", return_value=42)
    @patch("app.api.webhook.check_rate_limit", return_value=True)
    @patch("app.api.webhook.check_msg_duplicate", return_value=True)
    @patch("app.api.webhook.decrypt_message")
    @patch("app.api.webhook.verify_signature", return_value=True)
    def test_post_duplicate_msg_short_circuits(
        self, mock_verify, mock_decrypt, mock_dup, mock_rate,
        mock_insert, mock_enq, client,
    ):
        mock_decrypt.return_value = _fake_plaintext()
        resp = client.post(
            "/webhook/wecom",
            params={"msg_signature": "s", "timestamp": "t", "nonce": "n"},
            content=_FAKE_XML,
        )
        assert resp.status_code == 200
        # 幂等命中 → 不应触达限流、写表、入队
        mock_rate.assert_not_called()
        mock_insert.assert_not_called()
        mock_enq.assert_not_called()

    @patch("app.api.webhook._async_rate_limit_notify")
    @patch("app.api.webhook.enqueue_message")
    @patch("app.api.webhook._insert_inbound_event", return_value=42)
    @patch("app.api.webhook.check_rate_limit", return_value=False)
    @patch("app.api.webhook.check_msg_duplicate", return_value=False)
    @patch("app.api.webhook.decrypt_message")
    @patch("app.api.webhook.verify_signature", return_value=True)
    def test_post_rate_limited_no_inbound_event_no_enqueue(
        self, mock_verify, mock_decrypt, mock_dup, mock_rate,
        mock_insert, mock_enq, mock_notify, client,
    ):
        mock_decrypt.return_value = _fake_plaintext()
        resp = client.post(
            "/webhook/wecom",
            params={"msg_signature": "s", "timestamp": "t", "nonce": "n"},
            content=_FAKE_XML,
        )
        assert resp.status_code == 200
        assert resp.text == "success"
        mock_insert.assert_not_called()
        mock_enq.assert_not_called()
        mock_notify.assert_called_once()


class TestRateLimitNotify:
    """P1-2：限流提示应走专用队列 + 60s 去重，不混入 send_retry。"""

    def test_notify_enqueues_to_dedicated_queue_on_first_hit(self):
        from app.api import webhook as webhook_mod
        r = MagicMock()
        r.set.return_value = True  # SETNX 首次成功
        with patch("app.api.webhook.get_redis", return_value=r):
            webhook_mod._async_rate_limit_notify("u1")
        # 使用 SETNX 去重
        r.set.assert_called_once()
        set_args, set_kwargs = r.set.call_args
        assert "rate_limit_notified:u1" in set_args[0]
        assert set_kwargs.get("nx") is True
        # push 到专用队列，而不是 send_retry
        r.rpush.assert_called_once()
        push_args, _ = r.rpush.call_args
        assert push_args[0] == "queue:rate_limit_notify"
        import json
        payload = json.loads(push_args[1])
        assert payload["userid"] == "u1"
        assert payload["source"] == "rate_limit_notify"

    def test_notify_dedups_within_window(self):
        from app.api import webhook as webhook_mod
        r = MagicMock()
        r.set.return_value = None  # SETNX 已存在：被去重
        with patch("app.api.webhook.get_redis", return_value=r):
            webhook_mod._async_rate_limit_notify("u1")
        r.set.assert_called_once()
        # 去重窗口内不重复 push
        r.rpush.assert_not_called()

    @patch("app.api.webhook.enqueue_message")
    @patch("app.api.webhook._insert_inbound_event", return_value=42)
    @patch("app.api.webhook.check_rate_limit", return_value=True)
    @patch("app.api.webhook.check_msg_duplicate", return_value=False)
    @patch("app.api.webhook.decrypt_message")
    @patch("app.api.webhook.verify_signature", return_value=True)
    def test_post_happy_path_writes_event_and_enqueues(
        self, mock_verify, mock_decrypt, mock_dup, mock_rate,
        mock_insert, mock_enq, client,
    ):
        mock_decrypt.return_value = _fake_plaintext(
            content="苏州找电子厂", msg_id="abc", from_user="u1", msg_type="text",
        )
        resp = client.post(
            "/webhook/wecom",
            params={"msg_signature": "s", "timestamp": "t", "nonce": "n"},
            content=_FAKE_XML,
        )
        assert resp.status_code == 200
        mock_insert.assert_called_once()
        mock_enq.assert_called_once()
        # 检查入队的消息载荷包含关键字段
        queued_json, queue_name = mock_enq.call_args[0]
        import json
        payload = json.loads(queued_json)
        assert payload["msg_id"] == "abc"
        assert payload["from_userid"] == "u1"
        assert payload["msg_type"] == "text"
        assert payload["content"] == "苏州找电子厂"
        assert payload["inbound_event_id"] == 42

    @patch("app.api.webhook.enqueue_message", side_effect=Exception("redis down"))
    @patch("app.api.webhook._insert_inbound_event", return_value=42)
    @patch("app.api.webhook.check_rate_limit", return_value=True)
    @patch("app.api.webhook.check_msg_duplicate", return_value=False)
    @patch("app.api.webhook.decrypt_message")
    @patch("app.api.webhook.verify_signature", return_value=True)
    def test_post_enqueue_failure_still_returns_200(
        self, mock_verify, mock_decrypt, mock_dup, mock_rate,
        mock_insert, mock_enq, client,
    ):
        mock_decrypt.return_value = _fake_plaintext()
        resp = client.post(
            "/webhook/wecom",
            params={"msg_signature": "s", "timestamp": "t", "nonce": "n"},
            content=_FAKE_XML,
        )
        assert resp.status_code == 200
        assert resp.text == "success"

    @patch("app.api.webhook.enqueue_message")
    @patch("app.api.webhook._insert_inbound_event", return_value=42)
    @patch("app.api.webhook.check_rate_limit", return_value=True)
    @patch("app.api.webhook.check_msg_duplicate",
           side_effect=Exception("redis down"))
    @patch("app.api.webhook.decrypt_message")
    @patch("app.api.webhook.verify_signature", return_value=True)
    def test_post_redis_dedup_failure_does_not_block_flow(
        self, mock_verify, mock_decrypt, mock_dup, mock_rate,
        mock_insert, mock_enq, client,
    ):
        """L1 Redis 幂等失败时应降级到 L2（inbound_event 唯一约束）。"""
        mock_decrypt.return_value = _fake_plaintext()
        resp = client.post(
            "/webhook/wecom",
            params={"msg_signature": "s", "timestamp": "t", "nonce": "n"},
            content=_FAKE_XML,
        )
        assert resp.status_code == 200
        # 降级后仍然应该尝试写表和入队，由 UNIQUE(msg_id) 兜底去重
        mock_insert.assert_called_once()
        mock_enq.assert_called_once()


# ---------------------------------------------------------------------------
# 限流参数读取（内存缓存）
# ---------------------------------------------------------------------------

class TestRateLimitParams:
    """Phase 5 起：webhook 通过 Redis `config_cache:{key}` 共享配置缓存，

    不再维护进程内 dict，因此 system_config_service 更新配置后可立即命中新值。
    """

    def test_default_values_when_db_unavailable(self):
        from app.api import webhook as webhook_mod

        with (
            patch("app.api.webhook.get_cached_config", return_value=None),
            patch("app.api.webhook.set_cached_config"),
            patch("app.api.webhook.SessionLocal") as mock_session_factory,
        ):
            db = MagicMock()
            db.query.return_value.filter.return_value.first.return_value = None
            mock_session_factory.return_value = db
            window, max_count = webhook_mod._get_rate_limit_params()
            assert window == 10  # 默认
            assert max_count == 5

    def test_redis_cache_hit_skips_db(self):
        from app.api import webhook as webhook_mod

        def cache_get(key):
            return {"rate_limit.window_seconds": "20", "rate_limit.max_count": "7"}.get(key)

        with (
            patch("app.api.webhook.get_cached_config", side_effect=cache_get),
            patch("app.api.webhook.set_cached_config") as mock_set,
            patch("app.api.webhook.SessionLocal") as mock_session_factory,
        ):
            window, max_count = webhook_mod._get_rate_limit_params()
            assert window == 20
            assert max_count == 7
            # 命中 Redis 缓存后不应再开 DB 连接，也不需要回填 cache
            mock_session_factory.assert_not_called()
            mock_set.assert_not_called()

    def test_redis_cache_miss_falls_back_to_db_and_backfills(self):
        from app.api import webhook as webhook_mod

        def factory():
            db = MagicMock()
            cfg = MagicMock()
            cfg.config_value = "15"
            db.query.return_value.filter.return_value.first.return_value = cfg
            return db

        with (
            patch("app.api.webhook.get_cached_config", return_value=None),
            patch("app.api.webhook.set_cached_config") as mock_set,
            patch("app.api.webhook.SessionLocal", side_effect=factory),
        ):
            window, max_count = webhook_mod._get_rate_limit_params()
            assert window == 15
            assert max_count == 15
            # 每个 key 都应回填 Redis
            assert mock_set.call_count == 2
