"""字段契约快照测试（严格 == 断言）。

这些测试是本沙箱的"安全绳"：未来任何改动如果偷偷改字段名、大小写或
结构，都会在这里炸出来。断言使用 `==` 而非 `>=` / `issubset`，以精确
锁定契约。
"""
import re


# ============================================================================
# 顶层响应结构
# ============================================================================

class TestTopLevelShape:
    def test_users_top_level(self, client):
        data = client.get("/mock/wework/users").json()
        assert set(data.keys()) == {"errcode", "errmsg", "users"}

    def test_code2userinfo_top_level(self, client):
        data = client.get(
            "/mock/wework/code2userinfo",
            params={"access_token": "x", "code": "y"},
        ).json()
        assert set(data.keys()) == {"errcode", "errmsg", "external_userid", "openid"}

    def test_inbound_success_top_level(self, client, seeded_users, fakeredis_conn):
        data = client.post("/mock/wework/inbound", json={
            "ToUserName": "c", "FromUserName": "wm_mock_worker_001",
            "CreateTime": 1, "MsgType": "text", "Content": "hi",
            "MsgId": "contract_top_level", "AgentID": "a",
        }).json()
        assert set(data.keys()) == {"errcode", "errmsg", "msgid"}

    def test_inbound_error_top_level(self, client):
        data = client.post("/mock/wework/inbound", json={}).json()
        # 错误响应只有 errcode / errmsg
        assert set(data.keys()) == {"errcode", "errmsg"}

    def test_errcode_errmsg_always_present(self, client, seeded_users):
        """所有响应顶层都必须有 errcode + errmsg。"""
        responses = [
            client.get("/mock/wework/users").json(),
            client.get("/mock/wework/code2userinfo", params={"access_token": "x", "code": "y"}).json(),
        ]
        for r in responses:
            assert "errcode" in r
            assert "errmsg" in r


# ============================================================================
# 字段命名规范
# ============================================================================

class TestSnakeCaseCompliance:
    def test_no_camel_case_in_get_responses(self, client, seeded_users):
        """所有 GET 响应的 key 必须 snake_case（企微契约的外部联系人字段规范）。"""
        targets = [
            ("/mock/wework/users", {}),
            ("/mock/wework/code2userinfo", {"access_token": "x", "code": "y"}),
        ]
        for path, params in targets:
            data = client.get(path, params=params).json()
            _assert_all_keys_snake_case(data, path)


def _assert_all_keys_snake_case(obj, source: str):
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert re.fullmatch(r"[a-z_]+", k), f"{source} has non-snake_case key: {k!r}"
            _assert_all_keys_snake_case(v, source)
    elif isinstance(obj, list):
        for x in obj:
            _assert_all_keys_snake_case(x, source)


# ============================================================================
# 企微黑话锁死（禁用词 / 必用词）
# ============================================================================

class TestForbiddenAndRequiredFields:
    def test_no_userid_field_leaked(self, client, seeded_users):
        """JobBridge 面向外部联系人，全程用 external_userid，
        mock 接口**禁止**出现 'userid'（会和企业成员 userid 混淆）。
        """
        responses = [
            client.get("/mock/wework/users").json(),
            client.get("/mock/wework/code2userinfo", params={"access_token": "x", "code": "y"}).json(),
        ]
        for data in responses:
            _assert_key_not_in(data, "userid")

    def test_external_userid_is_wm_mock_prefix(self, client, seeded_users):
        for u in client.get("/mock/wework/users").json()["users"]:
            assert u["external_userid"].startswith("wm_mock_")


def _assert_key_not_in(obj, banned_key: str):
    if isinstance(obj, dict):
        assert banned_key not in obj, f"banned key {banned_key!r} found at top/nested level"
        for v in obj.values():
            _assert_key_not_in(v, banned_key)
    elif isinstance(obj, list):
        for x in obj:
            _assert_key_not_in(x, banned_key)


# ============================================================================
# 入队 payload 契约（必须与主后端 webhook.py:193-201 一致）
# ============================================================================

class TestQueuePayloadContract:
    """主后端 backend/app/api/webhook.py:193-201 的入队字段集合：
       msg_id / from_userid / msg_type / content / media_id /
       create_time / inbound_event_id
    """

    def test_queue_msg_keys_exact(self, client, seeded_users, fakeredis_conn):
        import json as json_lib
        r = client.post("/mock/wework/inbound", json={
            "ToUserName": "wwmock_corpid",
            "FromUserName": "wm_mock_worker_002",
            "CreateTime": 1713500000,
            "MsgType": "text",
            "Content": "contract queue key check",
            "MsgId": "contract_queue_exact",
            "AgentID": "1000002",
        })
        assert r.json()["errcode"] == 0
        queued = fakeredis_conn.lrange("queue:incoming", 0, -1)
        assert len(queued) == 1
        queue_msg = json_lib.loads(queued[0])
        assert set(queue_msg.keys()) == {
            "msg_id", "from_userid", "msg_type", "content",
            "media_id", "create_time", "inbound_event_id",
        }
        # 严格检查类型
        assert isinstance(queue_msg["msg_id"], str)
        assert isinstance(queue_msg["from_userid"], str)
        assert isinstance(queue_msg["msg_type"], str)
        assert isinstance(queue_msg["content"], str)
        assert queue_msg["media_id"] is None
        assert isinstance(queue_msg["create_time"], int)
        # inbound_event_id 可能是 int 或 None；sqlite 下是 int
        assert queue_msg["inbound_event_id"] is None or isinstance(queue_msg["inbound_event_id"], int)


# ============================================================================
# 入站 payload 大小写规范（对应企微 XML 解密后字段）
# ============================================================================

class TestInboundPayloadCaseContract:
    """企微回调 XML 字段是 Pascal 驼峰（ToUserName 等）；
    我们的 mock /inbound 请求体也必须用 Pascal，小写驼峰会被拒收。
    """

    def test_lowercase_variant_rejected(self, client, seeded_users):
        """如果误把 ToUserName 写成 touser / tousername，应返回 40001。"""
        bad_payload = {
            "tousername": "wwmock",
            "fromusername": "wm_mock_worker_001",
            "createtime": 1,
            "msgtype": "text",
            "content": "x",
            "msgid": "case_lower",
            "agentid": "1000002",
        }
        data = client.post("/mock/wework/inbound", json=bad_payload).json()
        assert data["errcode"] == 40001
