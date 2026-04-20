"""Mock 企业微信测试台 · 路由功能测试。"""
import json
import re

import pytest


# ============================================================================
# GET /mock/wework/users
# ============================================================================

class TestListUsers:
    def test_empty_returns_empty_list(self, client):
        r = client.get("/mock/wework/users")
        assert r.status_code == 200
        data = r.json()
        assert data["errcode"] == 0
        assert data["errmsg"] == "ok"
        assert data["users"] == []

    def test_returns_all_wm_mock_users(self, client, seeded_users):
        r = client.get("/mock/wework/users")
        assert r.status_code == 200
        data = r.json()
        assert len(data["users"]) == 4
        assert {u["role"] for u in data["users"]} == {"worker", "factory", "broker"}
        for u in data["users"]:
            assert u["external_userid"].startswith("wm_mock_")

    def test_does_not_expose_real_users(self, client, db):
        """即使 DB 里有真实用户（非 wm_mock_ 前缀），接口也不返回。"""
        from models import MockUser
        db.merge(MockUser(external_userid="real_user_xyz", role="worker", display_name="真实用户"))
        db.commit()
        r = client.get("/mock/wework/users")
        assert r.status_code == 200
        assert r.json()["users"] == []

    def test_response_fields_exact_set(self, client, seeded_users):
        """字段契约：顶层和每个 user 的字段集合严格等于预期。"""
        data = client.get("/mock/wework/users").json()
        assert set(data.keys()) == {"errcode", "errmsg", "users"}
        for u in data["users"]:
            assert set(u.keys()) == {"external_userid", "name", "role", "avatar"}


# ============================================================================
# GET /mock/wework/oauth2/authorize
# ============================================================================

class TestAuthorize:
    def test_redirects_with_code(self, client):
        r = client.get(
            "/mock/wework/oauth2/authorize",
            params={
                "appid": "wwX",
                "redirect_uri": "http://example.com/cb",
                "state": "abc",
            },
            follow_redirects=False,
        )
        assert r.status_code == 302
        loc = r.headers["location"]
        assert loc.startswith("http://example.com/cb")
        assert "code=MOCK_CODE_" in loc
        assert "state=abc" in loc

    def test_preserves_existing_query_in_redirect_uri(self, client):
        r = client.get(
            "/mock/wework/oauth2/authorize",
            params={
                "appid": "wwX",
                "redirect_uri": "http://example.com/cb?existing=1",
            },
            follow_redirects=False,
        )
        assert r.status_code == 302
        loc = r.headers["location"]
        # 原有 query 保留 + 新 code 追加
        assert "existing=1" in loc
        assert "code=MOCK_CODE_" in loc

    def test_no_state_when_not_provided(self, client):
        r = client.get(
            "/mock/wework/oauth2/authorize",
            params={"appid": "wwX", "redirect_uri": "http://example.com/cb"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "state=" not in r.headers["location"]


# ============================================================================
# GET /mock/wework/code2userinfo
# ============================================================================

class TestCode2Userinfo:
    def test_response_fields_exact_set(self, client):
        r = client.get(
            "/mock/wework/code2userinfo",
            params={"access_token": "MOCK", "code": "anything"},
        )
        assert r.status_code == 200
        data = r.json()
        assert set(data.keys()) == {"errcode", "errmsg", "external_userid", "openid"}
        assert data["errcode"] == 0
        assert data["external_userid"].startswith("wm_mock_")

    def test_all_keys_are_snake_case(self, client):
        """企微契约：字段名全部小写 + 下划线，无驼峰。"""
        data = client.get(
            "/mock/wework/code2userinfo",
            params={"access_token": "x", "code": "y"},
        ).json()
        for k in data.keys():
            assert re.fullmatch(r"[a-z_]+", k), f"key {k!r} violates snake_case contract"

    def test_x_mock_override_switches_identity(self, client):
        """前端通过 x_mock_external_userid 透传身份源。"""
        r = client.get(
            "/mock/wework/code2userinfo",
            params={
                "access_token": "x",
                "code": "y",
                "x_mock_external_userid": "wm_mock_factory_001",
            },
        )
        assert r.json()["external_userid"] == "wm_mock_factory_001"

    def test_no_userid_field_anywhere(self, client):
        """JobBridge 全程 external_userid，不应泄漏 'userid' 字段。"""
        data = client.get(
            "/mock/wework/code2userinfo",
            params={"access_token": "x", "code": "y"},
        ).json()

        def _all_keys(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    yield k
                    yield from _all_keys(v)
            elif isinstance(obj, list):
                for x in obj:
                    yield from _all_keys(x)

        assert "userid" not in set(_all_keys(data))


# ============================================================================
# POST /mock/wework/inbound
# ============================================================================

def _valid_inbound_payload(msg_id: str = "mock_msgid_xyz", content: str = "hello"):
    return {
        "ToUserName":   "wwmock_corpid",
        "FromUserName": "wm_mock_worker_001",
        "CreateTime":   1713500000,
        "MsgType":      "text",
        "Content":      content,
        "MsgId":        msg_id,
        "AgentID":      "1000002",
    }


class TestInbound:
    def test_happy_path(self, client, seeded_users, fakeredis_conn):
        payload = _valid_inbound_payload()
        r = client.post("/mock/wework/inbound", json=payload)
        assert r.status_code == 200
        data = r.json()
        assert data["errcode"] == 0
        assert data["errmsg"] == "ok"
        assert data["msgid"] == "mock_msgid_xyz"

        # Redis queue:incoming 收到一条消息
        queued = fakeredis_conn.lrange("queue:incoming", 0, -1)
        assert len(queued) == 1
        queue_msg = json.loads(queued[0])
        # 字段契约：必须与主后端 webhook.py:193-201 完全一致
        assert set(queue_msg.keys()) == {
            "msg_id", "from_userid", "msg_type", "content",
            "media_id", "create_time", "inbound_event_id",
        }
        assert queue_msg["msg_id"] == "mock_msgid_xyz"
        assert queue_msg["from_userid"] == "wm_mock_worker_001"
        assert queue_msg["msg_type"] == "text"
        assert queue_msg["content"] == "hello"
        assert queue_msg["media_id"] is None

    def test_missing_field_returns_40001(self, client):
        payload = _valid_inbound_payload()
        del payload["MsgId"]
        r = client.post("/mock/wework/inbound", json=payload)
        assert r.status_code == 200
        data = r.json()
        assert data["errcode"] == 40001
        assert "MsgId" in data["errmsg"]

    def test_missing_multiple_fields(self, client):
        payload = {"ToUserName": "x"}  # 缺 6 个
        r = client.post("/mock/wework/inbound", json=payload)
        assert r.json()["errcode"] == 40001

    def test_create_time_must_be_int(self, client, seeded_users):
        payload = _valid_inbound_payload()
        payload["CreateTime"] = "not-an-int"
        r = client.post("/mock/wework/inbound", json=payload)
        assert r.json()["errcode"] == 40002

    def test_idempotent_dedupe_same_msg_id(self, client, seeded_users, fakeredis_conn):
        payload = _valid_inbound_payload("dup_id_1")
        r1 = client.post("/mock/wework/inbound", json=payload)
        r2 = client.post("/mock/wework/inbound", json=payload)
        assert r1.json()["errcode"] == 0
        assert r2.json()["errcode"] == 0
        # 第二次应被幂等 drop（errmsg 标明）
        assert "duplicate" in r2.json()["errmsg"]
        # queue 只有 1 条
        assert len(fakeredis_conn.lrange("queue:incoming", 0, -1)) == 1

    def test_unknown_msgtype_falls_to_other(self, client, seeded_users, fakeredis_conn):
        payload = _valid_inbound_payload("weird_type_msg")
        payload["MsgType"] = "weird_new_type"
        r = client.post("/mock/wework/inbound", json=payload)
        assert r.status_code == 200
        assert r.json()["errcode"] == 0
        queue_msg = json.loads(fakeredis_conn.lrange("queue:incoming", 0, -1)[-1])
        assert queue_msg["msg_type"] == "other"

    def test_rejects_non_mock_prefix_from_userid(self, client, seeded_users, fakeredis_conn):
        """防止污染真实 user 表：FromUserName 必须以 wm_mock_ 开头。"""
        payload = _valid_inbound_payload("guard_prefix_check")
        payload["FromUserName"] = "real_attacker_userid"
        r = client.post("/mock/wework/inbound", json=payload)
        assert r.status_code == 200
        data = r.json()
        assert data["errcode"] == 40003
        assert "wm_mock_" in data["errmsg"]
        # 应该完全短路：没有入队，没有写 event 表
        assert len(fakeredis_conn.lrange("queue:incoming", 0, -1)) == 0

    def test_db_l2_dedupe_when_redis_ttl_expires(self, client, seeded_users, fakeredis_conn):
        """Q4 测试：Redis L1 key 过期后，DB UNIQUE(msg_id) 作为 L2 兜底。

        模拟 TTL 过期：第一次入库后手动删除 Redis key，第二次重放应命中 DB 幂等。
        """
        payload = _valid_inbound_payload("db_l2_dedupe")
        r1 = client.post("/mock/wework/inbound", json=payload)
        assert r1.json()["errcode"] == 0
        assert r1.json()["errmsg"] == "ok"

        # 模拟 Redis L1 TTL 到期
        fakeredis_conn.delete(f"wecom:msg:{payload['MsgId']}")

        # 第二次投递：L1 没命中 → L2 应该命中 IntegrityError
        r2 = client.post("/mock/wework/inbound", json=payload)
        assert r2.json()["errcode"] == 0
        assert "duplicate in db" in r2.json()["errmsg"]

        # 队列仍只有 1 条
        assert len(fakeredis_conn.lrange("queue:incoming", 0, -1)) == 1

    def test_cjk_unicode_preserved_through_enqueue(self, client, seeded_users, fakeredis_conn):
        """Q4 测试：Content 含中文/emoji 时，序列化保持字面字符（ensure_ascii=False）。

        规避重构时误启 ensure_ascii=True 导致 Worker 端读到 \\uXXXX 转义。
        """
        import json as json_lib
        payload = _valid_inbound_payload("cjk_check")
        payload["Content"] = "你好，我想找深圳的打包工 👷"
        r = client.post("/mock/wework/inbound", json=payload)
        assert r.json()["errcode"] == 0

        queued_raw = fakeredis_conn.lrange("queue:incoming", 0, -1)[-1]
        # 原始 JSON 字符串必须包含中文字面，不应是 \uXXXX 转义
        assert "你好" in queued_raw
        assert "打包工" in queued_raw
        assert "\\u" not in queued_raw, f"CJK got escaped: {queued_raw[:200]}"

        # 反序列化也要能正常读回
        queue_msg = json_lib.loads(queued_raw)
        assert "👷" in queue_msg["content"]

    def test_event_msgtype_allows_missing_msgid(self, client, seeded_users, fakeredis_conn):
        """Q3 放宽：event 类型允许不带 MsgId（真企微 event 回调不带）。"""
        payload = _valid_inbound_payload("event_placeholder_will_be_dropped")
        del payload["MsgId"]
        del payload["Content"]
        payload["MsgType"] = "event"
        payload["Event"] = "subscribe"
        r = client.post("/mock/wework/inbound", json=payload)
        assert r.json()["errcode"] == 0
        queue_msg = __import__("json").loads(fakeredis_conn.lrange("queue:incoming", 0, -1)[-1])
        # 自动合成的 msg_id 包含 event key
        assert "event_subscribe_" in queue_msg["msg_id"]
        assert queue_msg["msg_type"] == "event"

    def test_image_msgtype_requires_media_id(self, client, seeded_users, fakeredis_conn):
        """Q3 放宽：image / voice / video / file 必须带 MediaId。"""
        payload = _valid_inbound_payload("image_no_media")
        payload["MsgType"] = "image"
        del payload["Content"]
        # MediaId 缺失 → 40004
        r = client.post("/mock/wework/inbound", json=payload)
        assert r.json()["errcode"] == 40004
        assert "MediaId" in r.json()["errmsg"]

        # 带上 MediaId → 正常入队，media_id 列被持久化
        payload["MsgId"] = "image_with_media"
        payload["MediaId"] = "MEDIA_abcdef123"
        r2 = client.post("/mock/wework/inbound", json=payload)
        assert r2.json()["errcode"] == 0
        queue_msg = __import__("json").loads(fakeredis_conn.lrange("queue:incoming", 0, -1)[-1])
        assert queue_msg["msg_type"] == "image"
        assert queue_msg["media_id"] == "MEDIA_abcdef123"


# ============================================================================
# GET /mock/wework/config —— Q6 单点真源
# ============================================================================

class TestConfig:
    def test_config_returns_corpid_agentid(self, client):
        r = client.get("/mock/wework/config")
        assert r.status_code == 200
        data = r.json()
        assert set(data.keys()) == {"errcode", "errmsg", "corpid", "agentid"}
        assert data["errcode"] == 0
        assert isinstance(data["corpid"], str) and len(data["corpid"]) > 0
        assert isinstance(data["agentid"], str) and len(data["agentid"]) > 0


# ============================================================================
# GET /mock/wework/sse
# ============================================================================

class TestSSE:
    def test_returns_event_stream_content_type(self, client, fakeredis_conn):
        # 只读 header；不消费 body（否则会永远阻塞）
        with client.stream(
            "GET",
            "/mock/wework/sse",
            params={"external_userid": "wm_mock_worker_001"},
        ) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            assert r.headers.get("x-accel-buffering") == "no"
            assert r.headers.get("cache-control") == "no-cache"
