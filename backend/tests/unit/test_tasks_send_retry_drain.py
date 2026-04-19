"""Phase 7 tasks/send_retry_drain.py 单元测试。

验证：
- ``enqueue_group_send_retry``：写入 JSON payload，含 backoff_until 字段
- ``check_backlog``：超阈值告警；阈值内不告警；只告警**积压队列**，不消费
- ``drain_group_send_retry`` 各分支：
  - 队列空 → noop
  - 未到 backoff_until → 重新入队尾，**不调用** WeComClient
  - 推送成功 → 不再入队
  - 推送失败 → retry_count+1 + 重新入队（含新 backoff）
  - retry_count 达到 MAX_GROUP_RETRY → 丢弃 + 写 ``group_send_failed_final``
  - payload 是非法 JSON → 写 ``group_send_invalid_payload``，不抛异常
"""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from app.tasks import send_retry_drain


def _ctx_acquired(value: bool):
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=value)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


@pytest.fixture
def fake_redis(monkeypatch):
    fake = MagicMock()
    monkeypatch.setattr(send_retry_drain, "get_redis", lambda: fake)
    return fake


# ---------------------------------------------------------------------------
# enqueue_group_send_retry
# ---------------------------------------------------------------------------

class TestEnqueueGroupSendRetry:
    def test_writes_json_payload_with_backoff(self, fake_redis):
        before = time.time()
        send_retry_drain.enqueue_group_send_retry("GroupA", "hello", retry_count=2, backoff=120)
        after = time.time()

        fake_redis.rpush.assert_called_once()
        queue, raw = fake_redis.rpush.call_args.args
        assert queue == send_retry_drain.QUEUE_GROUP_SEND_RETRY

        payload = json.loads(raw)
        assert payload["chat_id"] == "GroupA"
        assert payload["content"] == "hello"
        assert payload["retry_count"] == 2
        # backoff_until 必须落在 [before+120, after+120] 区间
        assert before + 120 <= payload["backoff_until"] <= after + 120

    def test_redis_failure_does_not_propagate(self, fake_redis):
        """rpush 抛异常时函数应吞掉，避免拖垮调用方（如 daily_report）。"""
        fake_redis.rpush.side_effect = RuntimeError("redis down")
        # 不抛异常即通过
        send_retry_drain.enqueue_group_send_retry("GroupA", "hello")


# ---------------------------------------------------------------------------
# check_backlog
# ---------------------------------------------------------------------------

class TestCheckBacklog:
    def test_over_threshold_alerts_per_queue(self, fake_redis, monkeypatch):
        monkeypatch.setattr(send_retry_drain.settings, "monitor_send_retry_threshold", 20)
        # 两个队列分别返回长度
        fake_redis.llen.side_effect = [50, 5]  # send_retry > 阈值；group_send_retry 阈值内

        with patch.object(send_retry_drain, "task_lock", return_value=_ctx_acquired(True)), \
             patch.object(send_retry_drain, "_alert") as mock_alert:
            send_retry_drain.check_backlog()

            assert mock_alert.call_count == 1
            # 告警 event 名前缀必须含 send_retry_backlog
            event = mock_alert.call_args.args[0]
            assert event.startswith("send_retry_backlog:")

    def test_both_under_threshold_no_alert(self, fake_redis, monkeypatch):
        monkeypatch.setattr(send_retry_drain.settings, "monitor_send_retry_threshold", 20)
        fake_redis.llen.side_effect = [5, 10]

        with patch.object(send_retry_drain, "task_lock", return_value=_ctx_acquired(True)), \
             patch.object(send_retry_drain, "_alert") as mock_alert:
            send_retry_drain.check_backlog()

            mock_alert.assert_not_called()

    def test_does_not_consume_queue(self, fake_redis, monkeypatch):
        """check_backlog 只 LLEN，不能调用 LPOP / RPUSH 之类的消费动作。"""
        monkeypatch.setattr(send_retry_drain.settings, "monitor_send_retry_threshold", 20)
        fake_redis.llen.return_value = 3

        with patch.object(send_retry_drain, "task_lock", return_value=_ctx_acquired(True)):
            send_retry_drain.check_backlog()

        fake_redis.lpop.assert_not_called()
        fake_redis.rpush.assert_not_called()


# ---------------------------------------------------------------------------
# drain_group_send_retry 各分支
# ---------------------------------------------------------------------------

class TestDrainGroupSendRetry:
    def test_empty_queue_noop(self, fake_redis):
        fake_redis.lpop.return_value = None

        with patch.object(send_retry_drain, "task_lock", return_value=_ctx_acquired(True)), \
             patch("app.wecom.client.WeComClient") as mock_client_cls:
            send_retry_drain.drain_group_send_retry()

            mock_client_cls.assert_not_called()
            fake_redis.rpush.assert_not_called()

    def test_skip_when_lock_not_acquired(self, fake_redis):
        with patch.object(send_retry_drain, "task_lock", return_value=_ctx_acquired(False)):
            send_retry_drain.drain_group_send_retry()

            fake_redis.lpop.assert_not_called()

    def test_not_yet_at_backoff_reenqueues_without_send(self, fake_redis):
        """payload 的 backoff_until 在未来 → 不发送，原样回队。"""
        future = time.time() + 1000
        payload = {"chat_id": "G", "content": "x", "retry_count": 1, "backoff_until": future}
        fake_redis.lpop.return_value = json.dumps(payload)

        with patch.object(send_retry_drain, "task_lock", return_value=_ctx_acquired(True)), \
             patch("app.wecom.client.WeComClient") as mock_client_cls:
            send_retry_drain.drain_group_send_retry()

            mock_client_cls.assert_not_called()
            fake_redis.rpush.assert_called_once()
            requeued = json.loads(fake_redis.rpush.call_args.args[1])
            assert requeued["retry_count"] == 1  # 未失败 → 不递增

    def test_send_success_does_not_reenqueue(self, fake_redis):
        payload = {"chat_id": "G", "content": "x", "retry_count": 0, "backoff_until": 0}
        fake_redis.lpop.return_value = json.dumps(payload)

        with patch.object(send_retry_drain, "task_lock", return_value=_ctx_acquired(True)), \
             patch("app.wecom.client.WeComClient") as mock_client_cls:
            client_inst = MagicMock()
            client_inst.send_text_to_group.return_value = True
            mock_client_cls.return_value = client_inst

            send_retry_drain.drain_group_send_retry()

            client_inst.send_text_to_group.assert_called_once_with("G", "x")
            fake_redis.rpush.assert_not_called()

    def test_send_failure_increments_retry_and_reenqueues(self, fake_redis):
        payload = {"chat_id": "G", "content": "x", "retry_count": 0, "backoff_until": 0}
        fake_redis.lpop.return_value = json.dumps(payload)

        with patch.object(send_retry_drain, "task_lock", return_value=_ctx_acquired(True)), \
             patch("app.wecom.client.WeComClient") as mock_client_cls:
            client_inst = MagicMock()
            client_inst.send_text_to_group.return_value = False
            mock_client_cls.return_value = client_inst

            send_retry_drain.drain_group_send_retry()

            fake_redis.rpush.assert_called_once()
            requeued = json.loads(fake_redis.rpush.call_args.args[1])
            assert requeued["retry_count"] == 1
            # backoff_until 必须严格大于 now（已经过了原 backoff_until=0）
            assert requeued["backoff_until"] > time.time()

    def test_max_retry_drops_payload(self, fake_redis):
        """retry_count 达到 MAX_GROUP_RETRY-1 再失败一次后丢弃，不再入队。"""
        payload = {
            "chat_id": "G",
            "content": "x",
            "retry_count": send_retry_drain.MAX_GROUP_RETRY - 1,
            "backoff_until": 0,
        }
        fake_redis.lpop.return_value = json.dumps(payload)

        with patch.object(send_retry_drain, "task_lock", return_value=_ctx_acquired(True)), \
             patch("app.wecom.client.WeComClient") as mock_client_cls, \
             patch.object(send_retry_drain, "log_event") as mock_log:
            client_inst = MagicMock()
            client_inst.send_text_to_group.return_value = False
            mock_client_cls.return_value = client_inst

            send_retry_drain.drain_group_send_retry()

            fake_redis.rpush.assert_not_called()
            # 必须打 group_send_failed_final 事件，便于运维事后追溯
            event_names = [c.args[0] for c in mock_log.call_args_list]
            assert "group_send_failed_final" in event_names

    def test_invalid_json_payload_is_logged_and_dropped(self, fake_redis):
        fake_redis.lpop.return_value = "not-a-json"

        with patch.object(send_retry_drain, "task_lock", return_value=_ctx_acquired(True)), \
             patch.object(send_retry_drain, "log_event") as mock_log, \
             patch("app.wecom.client.WeComClient") as mock_client_cls:
            send_retry_drain.drain_group_send_retry()

            event_names = [c.args[0] for c in mock_log.call_args_list]
            assert "group_send_invalid_payload" in event_names
            mock_client_cls.assert_not_called()
            fake_redis.rpush.assert_not_called()


# ---------------------------------------------------------------------------
# _alert dedupe（独立精简实现，与 worker_monitor._alert 行为一致）
# ---------------------------------------------------------------------------

class TestAlertDedupe:
    def test_first_time_pushes_to_group(self, fake_redis, monkeypatch):
        monkeypatch.setattr(send_retry_drain.settings, "daily_report_chat_id", "GroupX")
        fake_redis.set.return_value = True  # 首次

        with patch("app.wecom.client.WeComClient") as mock_client_cls:
            client_inst = MagicMock()
            client_inst.send_text_to_group.return_value = True
            mock_client_cls.return_value = client_inst

            send_retry_drain._alert("evt", "msg")

            client_inst.send_text_to_group.assert_called_once_with("GroupX", "msg")

    def test_within_window_does_not_push(self, fake_redis, monkeypatch):
        monkeypatch.setattr(send_retry_drain.settings, "daily_report_chat_id", "GroupX")
        fake_redis.set.return_value = False  # dedupe 命中

        with patch("app.wecom.client.WeComClient") as mock_client_cls:
            send_retry_drain._alert("evt", "msg")
            mock_client_cls.assert_not_called()

    def test_no_chat_id_skips_send(self, fake_redis, monkeypatch):
        monkeypatch.setattr(send_retry_drain.settings, "daily_report_chat_id", "")
        fake_redis.set.return_value = True

        with patch("app.wecom.client.WeComClient") as mock_client_cls:
            send_retry_drain._alert("evt", "msg")
            mock_client_cls.assert_not_called()

    def test_send_failure_enqueues_retry(self, fake_redis, monkeypatch):
        monkeypatch.setattr(send_retry_drain.settings, "daily_report_chat_id", "GroupX")
        fake_redis.set.return_value = True

        with patch("app.wecom.client.WeComClient") as mock_client_cls, \
             patch.object(send_retry_drain, "enqueue_group_send_retry") as mock_enqueue:
            client_inst = MagicMock()
            client_inst.send_text_to_group.return_value = False
            mock_client_cls.return_value = client_inst

            send_retry_drain._alert("evt", "msg")

            mock_enqueue.assert_called_once_with("GroupX", "msg")
