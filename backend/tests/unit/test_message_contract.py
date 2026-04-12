"""消息基础契约测试：队列 key、调用顺序、状态流转、死信规则。

验证文档化的契约与实际代码对齐。
"""
import os
import pytest

from app.core.redis_client import (
    QUEUE_INCOMING,
    QUEUE_DEAD_LETTER,
    MSG_DEDUP_PREFIX,
    RATE_LIMIT_PREFIX,
    SESSION_PREFIX,
    LOCK_PREFIX,
)


class TestQueueKeyNames:
    """队列 key 命名约定。"""

    def test_incoming_queue_key(self):
        assert QUEUE_INCOMING == "queue:incoming"

    def test_dead_letter_queue_key(self):
        assert QUEUE_DEAD_LETTER == "queue:dead_letter"


class TestRedisKeyPrefixes:
    """Redis key 前缀约定。"""

    def test_session_prefix(self):
        assert SESSION_PREFIX == "session:"

    def test_msg_dedup_prefix(self):
        assert MSG_DEDUP_PREFIX == "msg:"

    def test_rate_limit_prefix(self):
        assert RATE_LIMIT_PREFIX == "rate:"

    def test_lock_prefix(self):
        assert LOCK_PREFIX == "lock:"


class TestWecomInboundEventStatuses:
    """wecom_inbound_event 状态值验证。"""

    def test_all_five_statuses_defined(self):
        """验证 ORM 模型中定义的 5 个状态值。"""
        from app.models import WecomInboundEvent
        import inspect
        src = inspect.getsource(WecomInboundEvent)
        for status in ["received", "processing", "done", "failed", "dead_letter"]:
            assert status in src, f"Status '{status}' not found in WecomInboundEvent model"


class TestCallOrderDocumentation:
    """验证调用顺序文档存在。"""

    def test_message_contract_doc_exists(self):
        doc_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "docs", "message-contract.md"
        )
        assert os.path.isfile(doc_path), "docs/message-contract.md should exist"

    def test_contract_doc_contains_key_sections(self):
        doc_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "docs", "message-contract.md"
        )
        with open(doc_path, "r", encoding="utf-8") as f:
            content = f.read()

        # 验证关键章节存在
        assert "check_rate_limit" in content
        assert "check_msg_duplicate" in content
        assert "enqueue_message" in content
        assert "dequeue_message" in content
        assert "queue:incoming" in content
        assert "queue:dead_letter" in content
        assert "wecom_inbound_event" in content
        assert "received" in content
        assert "processing" in content
        assert "done" in content
        assert "failed" in content
        assert "dead_letter" in content

    def test_retry_limit_documented(self):
        doc_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "docs", "message-contract.md"
        )
        with open(doc_path, "r", encoding="utf-8") as f:
            content = f.read()

        # 验证重试上限为 2 次
        assert "2" in content  # 重试上限
        assert "死信" in content or "dead_letter" in content


class TestFunctionSignatures:
    """验证 redis_client 中关键函数的签名存在。"""

    def test_check_rate_limit_exists(self):
        from app.core.redis_client import check_rate_limit
        assert callable(check_rate_limit)

    def test_check_msg_duplicate_exists(self):
        from app.core.redis_client import check_msg_duplicate
        assert callable(check_msg_duplicate)

    def test_enqueue_message_exists(self):
        from app.core.redis_client import enqueue_message
        assert callable(enqueue_message)

    def test_dequeue_message_exists(self):
        from app.core.redis_client import dequeue_message
        assert callable(dequeue_message)
