"""Mock 企业微信测试台 · 出站总线单元测试。

fakeredis 对同进程的 pubsub 订阅行为支持较好；跨进程或持久化行为
不在本测试范围（SSE 端到端靠集成测试）。
"""
import json

import pytest


class TestPublishSubscribe:
    def test_publish_returns_subscriber_count(self, fakeredis_conn):
        import outbound_bus
        # 没有订阅者时 publish 返回 0
        n = outbound_bus.publish("wm_mock_worker_001", {"hello": "world"})
        assert n == 0

    def test_publish_received_by_subscriber(self, fakeredis_conn):
        import outbound_bus
        pubsub = outbound_bus.subscribe("wm_mock_worker_001")
        try:
            # 消费 subscribe ack
            pubsub.get_message(timeout=1.0)

            outbound_bus.publish("wm_mock_worker_001", {"foo": 42})

            # 轮询读一条业务消息
            msg = None
            for _ in range(20):
                m = pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
                if m and m.get("type") == "message":
                    msg = m
                    break

            assert msg is not None, "订阅者未收到 publish 消息"
            data = json.loads(msg["data"])
            assert data == {"foo": 42}
        finally:
            outbound_bus.unsubscribe(pubsub)

    def test_different_channels_isolated(self, fakeredis_conn):
        """给 A 频道 publish，订阅 B 的不应收到。"""
        import outbound_bus
        pubsub_b = outbound_bus.subscribe("wm_mock_worker_B")
        try:
            pubsub_b.get_message(timeout=1.0)  # ack
            outbound_bus.publish("wm_mock_worker_A", {"payload": "for_A"})

            # B 频道不应收到
            for _ in range(10):
                m = pubsub_b.get_message(ignore_subscribe_messages=True, timeout=0.05)
                if m and m.get("type") == "message":
                    pytest.fail(f"B should not receive A's message: {m}")
        finally:
            outbound_bus.unsubscribe(pubsub_b)

    def test_group_channel_key_format(self, fakeredis_conn):
        """群消息 target_key 约定是 'chat:{chat_id}'。"""
        import outbound_bus
        pubsub = outbound_bus.subscribe("chat:gid_test_123")
        try:
            pubsub.get_message(timeout=1.0)
            outbound_bus.publish("chat:gid_test_123", {"group": True})

            msg = None
            for _ in range(20):
                m = pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
                if m and m.get("type") == "message":
                    msg = m
                    break

            assert msg is not None
            assert json.loads(msg["data"])["group"] is True
        finally:
            outbound_bus.unsubscribe(pubsub)


class TestUnsubscribeSwallowsErrors:
    def test_double_unsubscribe_is_safe(self, fakeredis_conn):
        import outbound_bus
        pubsub = outbound_bus.subscribe("wm_mock_worker_001")
        outbound_bus.unsubscribe(pubsub)
        # 第二次调用不应抛
        outbound_bus.unsubscribe(pubsub)
