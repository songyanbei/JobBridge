"""worker 单元测试（Phase 4）。

通过 mock 关键依赖（redis_client、message_router、WeComClient、SessionLocal）
验证：
- 消息处理 happy path
- 重试 / 死信流程
- 出站错误分类（token 过期刷新、用户不存在标记 inactive、限流进 send_retry）
- send_retry 队列消费（退避、最终放弃写 audit_log）
- 启动自检重入队列
- 心跳写入
"""
import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from app.schemas.conversation import ReplyMessage
from app.services.worker import (
    MAX_RETRY,
    MAX_SEND_RETRY,
    QUEUE_SEND_RETRY,
    SEND_RETRY_BACKOFFS,
    Worker,
    _build_wecom_message,
    _coerce_log_msg_type,
)
from app.wecom.client import WeComError


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _basic_msg_data(msg_id="m1", userid="u1", content="你好", event_id=42):
    return {
        "msg_id": msg_id,
        "from_userid": userid,
        "msg_type": "text",
        "content": content,
        "media_id": "",
        "create_time": 1700000000,
        "inbound_event_id": event_id,
    }


@pytest.fixture
def worker():
    with patch("app.services.worker.get_redis"), \
         patch("app.services.worker.WeComClient"):
        return Worker()


# ---------------------------------------------------------------------------
# _build_wecom_message
# ---------------------------------------------------------------------------

class TestBuildMessage:
    def test_build_text(self):
        msg = _build_wecom_message(_basic_msg_data())
        assert msg.msg_id == "m1"
        assert msg.from_user == "u1"
        assert msg.msg_type == "text"
        assert msg.content == "你好"
        assert msg.image_url == ""


class TestCoerceLogMsgType:
    def test_text_passthrough(self):
        assert _coerce_log_msg_type("text") == "text"

    def test_image_passthrough(self):
        assert _coerce_log_msg_type("image") == "image"

    def test_file_maps_to_system(self):
        assert _coerce_log_msg_type("file") == "system"

    def test_event_maps_to_system(self):
        assert _coerce_log_msg_type("event") == "system"

    def test_unknown_maps_to_system(self):
        assert _coerce_log_msg_type("weird") == "system"


# ---------------------------------------------------------------------------
# 消息处理 happy path
# ---------------------------------------------------------------------------

class TestProcessMessageHappyPath:
    @patch("app.services.worker.SessionLocal")
    @patch("app.services.worker.message_router")
    @patch("app.services.worker.user_lock")
    def test_happy_path_processes_and_marks_done(
        self, mock_lock_cm, mock_router, mock_session_factory, worker,
    ):
        # user_lock 返回 acquired=True
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=True)
        cm.__exit__ = MagicMock(return_value=False)
        mock_lock_cm.return_value = cm

        mock_router.process.return_value = [
            ReplyMessage(userid="u1", content="hello"),
        ]

        db = MagicMock()
        mock_session_factory.return_value = db

        # 发送成功
        worker._wecom_client.send_text.return_value = {"errcode": 0}

        worker._process_message(_basic_msg_data())

        mock_router.process.assert_called_once()
        worker._wecom_client.send_text.assert_called_once_with("u1", "hello")
        # 至少 commit 3 次（router、log、done）
        assert db.commit.call_count >= 3


# ---------------------------------------------------------------------------
# 锁竞争重入
# ---------------------------------------------------------------------------

class TestLockContention:
    @patch("app.services.worker.enqueue_message")
    @patch("app.services.worker.user_lock")
    def test_lock_busy_requeues(self, mock_lock_cm, mock_enq, worker):
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=False)
        cm.__exit__ = MagicMock(return_value=False)
        mock_lock_cm.return_value = cm

        with patch("app.services.worker.time.sleep"):
            worker._process_message(_basic_msg_data())

        mock_enq.assert_called_once()
        args, _ = mock_enq.call_args
        payload = json.loads(args[0])
        assert payload["from_userid"] == "u1"


# ---------------------------------------------------------------------------
# 错误处理：重试 / 死信
# ---------------------------------------------------------------------------

class TestErrorHandling:
    @patch("app.services.worker.enqueue_message")
    def test_retry_under_max_requeues_with_incremented_count(
        self, mock_enq, worker,
    ):
        with patch.object(worker, "_mark_event_fail") as mock_fail, \
             patch.object(worker, "_update_retry_and_error_keep_processing") as mock_keep:
            worker._handle_error(
                _basic_msg_data(), event_id=42, retry_count=0,
                error=RuntimeError("boom"),
            )
        mock_enq.assert_called_once()
        args, _ = mock_enq.call_args
        payload = json.loads(args[0])
        assert payload["_retry_count"] == 1
        mock_fail.assert_called_once()
        mock_keep.assert_not_called()

    @patch("app.services.worker.enqueue_message")
    def test_retry_at_max_goes_to_dead_letter(self, mock_enq, worker):
        with patch.object(worker, "_mark_event_fail"):
            worker._handle_error(
                _basic_msg_data(), event_id=42, retry_count=MAX_RETRY,
                error=RuntimeError("boom"),
            )
        # 推入死信队列
        args, _ = mock_enq.call_args
        from app.services.worker import QUEUE_DEAD_LETTER
        assert args[1] == QUEUE_DEAD_LETTER
        # 尝试发送兜底回复
        worker._wecom_client.send_text.assert_called()

    @patch("app.services.worker.enqueue_message", side_effect=Exception("redis down"))
    def test_p0_1_retry_enqueue_failure_keeps_processing_status(
        self, mock_enq, worker,
    ):
        """P0-1：retry 阶段入队失败时不得把 status 改成 failed，
        必须保持 processing 让 startup_recovery 兜底重入队，避免消息丢失。"""
        with patch.object(worker, "_mark_event_fail") as mock_fail, \
             patch.object(
                 worker, "_update_retry_and_error_keep_processing"
             ) as mock_keep:
            worker._handle_error(
                _basic_msg_data(), event_id=42, retry_count=0,
                error=RuntimeError("boom"),
            )
        # 入队失败：不应标 failed（那会让 startup_recovery 扫不到）
        mock_fail.assert_not_called()
        # 应该只更新 retry_count/error，保持 processing
        mock_keep.assert_called_once()
        args, kwargs = mock_keep.call_args
        assert args[0] == 42
        assert args[1] == 1  # retry_count + 1


# ---------------------------------------------------------------------------
# 出站错误分类
# ---------------------------------------------------------------------------

class TestSendErrorHandling:
    def test_token_expired_refreshes_and_retries(self, worker):
        """P1-3：token 过期时应走公开 invalidate_token()，不再触碰私有字段。"""
        call_count = {"n": 0}

        def _send(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise WeComError("token expired", errcode=42001)
            return {"errcode": 0}

        worker._wecom_client.send_text.side_effect = _send
        reply = ReplyMessage(userid="u1", content="hi")
        ok = worker._send_one(reply)
        assert ok is True
        assert call_count["n"] == 2
        # 通过公开方法失效缓存，而非直接改私有属性
        worker._wecom_client.invalidate_token.assert_called_once()

    @patch("app.services.worker.SessionLocal")
    def test_user_unreachable_marks_inactive_no_retry(self, mock_factory, worker):
        db = MagicMock()
        user = MagicMock()
        user.extra = None  # 初始 extra 为 NULL
        db.query.return_value.filter.return_value.first.return_value = user
        mock_factory.return_value = db
        worker._wecom_client.send_text.side_effect = WeComError(
            "user not found", errcode=60111,
        )
        reply = ReplyMessage(userid="u1", content="hi")
        ok = worker._send_one(reply)
        assert ok is False
        # 不再修改 user.status（保持 active），只打 extra 标
        assert user.extra is not None
        assert user.extra.get("wecom_unreachable") is True
        assert "wecom_unreachable_at" in user.extra
        db.commit.assert_called_once()

    def test_rate_limit_enqueues_send_retry(self, worker):
        worker._wecom_client.send_text.side_effect = WeComError(
            "rate limited", errcode=45009,
        )
        worker._redis.rpush = MagicMock()
        reply = ReplyMessage(userid="u1", content="hi")
        ok = worker._send_one(reply)
        assert ok is False
        worker._redis.rpush.assert_called_once()
        args, _ = worker._redis.rpush.call_args
        assert args[0] == QUEUE_SEND_RETRY

    def test_generic_exception_enqueues_send_retry(self, worker):
        worker._wecom_client.send_text.side_effect = RuntimeError("network down")
        worker._redis.rpush = MagicMock()
        reply = ReplyMessage(userid="u1", content="hi")
        ok = worker._send_one(reply)
        assert ok is False
        worker._redis.rpush.assert_called_once()


# ---------------------------------------------------------------------------
# send_retry 队列消费
# ---------------------------------------------------------------------------

class TestRateLimitNotifyQueue:
    """P1-2：限流通知走专用队列，发失败即丢，不与 send_retry 混用。"""

    def test_best_effort_send_success(self, worker):
        payload = {"userid": "u1", "content": "您发送太频繁了"}
        worker._redis.lpop = MagicMock(return_value=json.dumps(payload))
        worker._redis.rpush = MagicMock()
        worker._wecom_client.send_text.return_value = {"errcode": 0}

        worker._process_rate_limit_notify_once()

        worker._wecom_client.send_text.assert_called_once_with("u1", "您发送太频繁了")
        # 不应重入任何队列
        worker._redis.rpush.assert_not_called()

    def test_send_failure_drops_without_retry(self, worker):
        payload = {"userid": "u1", "content": "您发送太频繁了"}
        worker._redis.lpop = MagicMock(return_value=json.dumps(payload))
        worker._redis.rpush = MagicMock()
        worker._wecom_client.send_text.side_effect = RuntimeError("network")

        worker._process_rate_limit_notify_once()

        # 静默丢弃，绝不重试入 send_retry
        worker._redis.rpush.assert_not_called()


class TestSendRetryQueue:
    def test_backoff_not_reached_requeues_without_send(self, worker):
        payload = {
            "userid": "u1", "content": "x",
            "send_retry_count": 0,
            "backoff_until": time.time() + 60,  # 还未到
        }
        worker._redis.lpop = MagicMock(return_value=json.dumps(payload))
        worker._redis.rpush = MagicMock()

        with patch("app.services.worker.time.sleep"):
            worker._process_send_retry_once()

        worker._redis.rpush.assert_called_once()
        worker._wecom_client.send_text.assert_not_called()

    def test_success_drops_from_queue(self, worker):
        payload = {
            "userid": "u1", "content": "x",
            "send_retry_count": 0,
            "backoff_until": 0,
        }
        worker._redis.lpop = MagicMock(return_value=json.dumps(payload))
        worker._redis.rpush = MagicMock()
        worker._wecom_client.send_text.return_value = {"errcode": 0}

        worker._process_send_retry_once()

        worker._wecom_client.send_text.assert_called_once_with("u1", "x")
        worker._redis.rpush.assert_not_called()

    def test_retry_at_max_writes_audit_log(self, worker):
        payload = {
            "userid": "u1", "content": "x",
            "send_retry_count": MAX_SEND_RETRY - 1,
            "backoff_until": 0,
        }
        worker._redis.lpop = MagicMock(return_value=json.dumps(payload))
        worker._redis.rpush = MagicMock()
        worker._wecom_client.send_text.side_effect = RuntimeError("perm fail")

        with patch.object(worker, "_write_send_failed_audit") as mock_audit:
            worker._process_send_retry_once()

        mock_audit.assert_called_once()
        # 达到上限后不再重入队列
        worker._redis.rpush.assert_not_called()


# ---------------------------------------------------------------------------
# 启动自检
# ---------------------------------------------------------------------------

class TestStartupRecovery:
    @patch("app.services.worker.enqueue_message")
    @patch("app.services.worker.SessionLocal")
    def test_requeues_processing_rows(self, mock_factory, mock_enq, worker):
        db = MagicMock()
        zombie = MagicMock()
        zombie.msg_id = "m1"
        zombie.from_userid = "u1"
        zombie.msg_type = "text"
        zombie.content_brief = "hello"
        zombie.media_id = None
        zombie.id = 42
        zombie.retry_count = 0
        from datetime import datetime, timezone
        zombie.created_at = datetime(2026, 4, 10, tzinfo=timezone.utc)
        db.query.return_value.filter.return_value.all.return_value = [zombie]
        mock_factory.return_value = db

        worker._startup_recovery()

        assert mock_enq.call_count == 1
        assert zombie.status == "received"
        assert zombie.worker_started_at is None
        db.commit.assert_called_once()

    @patch("app.services.worker.enqueue_message")
    @patch("app.services.worker.SessionLocal")
    def test_requeues_image_preserves_media_id(self, mock_factory, mock_enq, worker):
        """P0-2：图片消息恢复时 media_id 与原始 msg_type 必须回写到队列 payload。"""
        db = MagicMock()
        zombie = MagicMock()
        zombie.msg_id = "m-img"
        zombie.from_userid = "u1"
        zombie.msg_type = "image"
        zombie.content_brief = "[image] media_id saved"
        zombie.media_id = "MEDIA_XYZ"
        zombie.id = 99
        zombie.retry_count = 0
        from datetime import datetime, timezone
        zombie.created_at = datetime(2026, 4, 10, tzinfo=timezone.utc)
        db.query.return_value.filter.return_value.all.return_value = [zombie]
        mock_factory.return_value = db

        worker._startup_recovery()

        assert mock_enq.call_count == 1
        payload = json.loads(mock_enq.call_args[0][0])
        assert payload["msg_type"] == "image"
        assert payload["media_id"] == "MEDIA_XYZ"
        # 媒体消息不把 content_brief 透传为 text content
        assert payload["content"] == ""

    @patch("app.services.worker.enqueue_message")
    @patch("app.services.worker.SessionLocal")
    def test_requeues_file_preserves_raw_msg_type(self, mock_factory, mock_enq, worker):
        """P1-5：file/video/link/location 恢复时保持原始 msg_type，不再被 coerced 成 event。"""
        db = MagicMock()
        zombie = MagicMock()
        zombie.msg_id = "m-file"
        zombie.from_userid = "u1"
        zombie.msg_type = "file"
        zombie.content_brief = "[file] media_id saved"
        zombie.media_id = "FILE_XYZ"
        zombie.id = 100
        zombie.retry_count = 0
        from datetime import datetime, timezone
        zombie.created_at = datetime(2026, 4, 10, tzinfo=timezone.utc)
        db.query.return_value.filter.return_value.all.return_value = [zombie]
        mock_factory.return_value = db

        worker._startup_recovery()

        payload = json.loads(mock_enq.call_args[0][0])
        assert payload["msg_type"] == "file"
        assert payload["media_id"] == "FILE_XYZ"


# ---------------------------------------------------------------------------
# 心跳
# ---------------------------------------------------------------------------

class TestConversationLog:
    def test_inbound_and_outbound_written_with_snapshot(self, worker):
        """出站 ReplyMessage 的 intent/criteria_snapshot 应被写入 conversation_log。"""
        from app.services.worker import ConversationLog
        db = MagicMock()

        msg = _build_wecom_message(_basic_msg_data(msg_id="m1", userid="u1"))
        replies = [
            ReplyMessage(
                userid="u1", content="3 条结果",
                intent="search_job",
                criteria_snapshot={"criteria": {"city": ["苏州市"]}, "prompt_version": "v2.0"},
            ),
        ]
        worker._write_conversation_log(db, msg, replies)

        # nested transaction 用作 UNIQUE 冲突保护
        assert db.begin_nested.call_count == 2
        # 两次 add：入站 + 出站
        assert db.add.call_count == 2


class TestHeartbeat:
    def test_heartbeat_writes_key_with_ttl(self, worker):
        worker._redis.set = MagicMock()
        # 启动心跳线程并立即设置退出，避免实际循环 60s
        started = threading.Event()

        original_set = worker._redis.set

        def _set_wrapped(*args, **kwargs):
            original_set(*args, **kwargs)
            started.set()
            worker._running = False

        worker._redis.set = _set_wrapped
        worker._start_heartbeat()
        assert started.wait(timeout=3.0)
        # Thread 会检查 self._running 后退出
        worker._heartbeat_thread.join(timeout=5.0)
        assert not worker._heartbeat_thread.is_alive()
