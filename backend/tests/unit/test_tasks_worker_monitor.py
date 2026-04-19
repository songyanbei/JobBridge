"""Phase 7 tasks/worker_monitor.py 单元测试。

验证：
- ``check_heartbeat``：无心跳 → ``_alert(worker_all_offline)``；有心跳 → 不告警
- ``check_queue_backlog``：超阈值 → 告警；阈值内 → 不告警
- ``check_dead_letter``：> 0 → 告警；= 0 → 不告警
- ``_alert``：dedupe 窗口内首次推送、第二次只 log 不推
- ``_push_group``：``daily_report_chat_id`` 未配置时只 log 不实例化 WeComClient
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.tasks import worker_monitor


def _ctx_acquired(value: bool):
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=value)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


@pytest.fixture
def fake_redis(monkeypatch):
    fake = MagicMock()
    monkeypatch.setattr(worker_monitor, "get_redis", lambda: fake)
    return fake


# ---------------------------------------------------------------------------
# check_heartbeat
# ---------------------------------------------------------------------------

class TestCheckHeartbeat:
    def test_no_keys_triggers_alert(self, fake_redis):
        fake_redis.scan_iter.return_value = iter([])

        with patch.object(worker_monitor, "task_lock", return_value=_ctx_acquired(True)), \
             patch.object(worker_monitor, "_alert") as mock_alert:
            worker_monitor.check_heartbeat()

            mock_alert.assert_called_once()
            event_name = mock_alert.call_args.args[0]
            assert event_name == "worker_all_offline"

    def test_with_keys_no_alert(self, fake_redis):
        fake_redis.scan_iter.return_value = iter(["worker:heartbeat:abc", "worker:heartbeat:def"])

        with patch.object(worker_monitor, "task_lock", return_value=_ctx_acquired(True)), \
             patch.object(worker_monitor, "_alert") as mock_alert:
            worker_monitor.check_heartbeat()

            mock_alert.assert_not_called()

    def test_skip_when_lock_not_acquired(self, fake_redis):
        with patch.object(worker_monitor, "task_lock", return_value=_ctx_acquired(False)), \
             patch.object(worker_monitor, "_alert") as mock_alert:
            worker_monitor.check_heartbeat()

            # 未取锁就直接返回，连 redis.scan_iter 都不调
            fake_redis.scan_iter.assert_not_called()
            mock_alert.assert_not_called()


# ---------------------------------------------------------------------------
# check_queue_backlog
# ---------------------------------------------------------------------------

class TestCheckQueueBacklog:
    def test_over_threshold_alerts(self, fake_redis, monkeypatch):
        monkeypatch.setattr(worker_monitor.settings, "monitor_queue_incoming_threshold", 50)
        fake_redis.llen.return_value = 200

        with patch.object(worker_monitor, "task_lock", return_value=_ctx_acquired(True)), \
             patch.object(worker_monitor, "_alert") as mock_alert:
            worker_monitor.check_queue_backlog()

            mock_alert.assert_called_once()
            assert mock_alert.call_args.args[0] == "queue_backlog"

    def test_at_or_below_threshold_no_alert(self, fake_redis, monkeypatch):
        monkeypatch.setattr(worker_monitor.settings, "monitor_queue_incoming_threshold", 50)
        fake_redis.llen.return_value = 50  # 阈值边界：不告警（实现是 > 不是 >=）

        with patch.object(worker_monitor, "task_lock", return_value=_ctx_acquired(True)), \
             patch.object(worker_monitor, "_alert") as mock_alert:
            worker_monitor.check_queue_backlog()

            mock_alert.assert_not_called()


# ---------------------------------------------------------------------------
# check_dead_letter
# ---------------------------------------------------------------------------

class TestCheckDeadLetter:
    def test_nonzero_alerts(self, fake_redis):
        fake_redis.llen.return_value = 1

        with patch.object(worker_monitor, "task_lock", return_value=_ctx_acquired(True)), \
             patch.object(worker_monitor, "_alert") as mock_alert:
            worker_monitor.check_dead_letter()

            mock_alert.assert_called_once()
            assert mock_alert.call_args.args[0] == "dead_letter"

    def test_zero_no_alert(self, fake_redis):
        fake_redis.llen.return_value = 0

        with patch.object(worker_monitor, "task_lock", return_value=_ctx_acquired(True)), \
             patch.object(worker_monitor, "_alert") as mock_alert:
            worker_monitor.check_dead_letter()

            mock_alert.assert_not_called()


# ---------------------------------------------------------------------------
# _alert dedupe
# ---------------------------------------------------------------------------

class TestAlertDedupe:
    def test_first_time_pushes(self, fake_redis):
        fake_redis.set.return_value = True  # SETNX 成功 → 首次

        with patch.object(worker_monitor, "_push_group") as mock_push:
            worker_monitor._alert("evt", "boom")
            mock_push.assert_called_once_with("boom")

    def test_within_window_does_not_push(self, fake_redis):
        fake_redis.set.return_value = False  # 已经存在 dedupe key

        with patch.object(worker_monitor, "_push_group") as mock_push:
            worker_monitor._alert("evt", "boom")
            mock_push.assert_not_called()


# ---------------------------------------------------------------------------
# _push_group
# ---------------------------------------------------------------------------

class TestPushGroup:
    def test_skips_when_chat_id_empty(self, monkeypatch):
        monkeypatch.setattr(worker_monitor.settings, "daily_report_chat_id", "")

        with patch("app.wecom.client.WeComClient") as mock_client_cls:
            worker_monitor._push_group("anything")
            mock_client_cls.assert_not_called()

    def test_send_failure_enqueues_retry(self, monkeypatch):
        monkeypatch.setattr(worker_monitor.settings, "daily_report_chat_id", "GroupX")

        with patch("app.wecom.client.WeComClient") as mock_client_cls, \
             patch("app.tasks.send_retry_drain.enqueue_group_send_retry") as mock_enqueue:
            client_inst = MagicMock()
            client_inst.send_text_to_group.return_value = False
            mock_client_cls.return_value = client_inst

            worker_monitor._push_group("alert text")

            mock_enqueue.assert_called_once_with("GroupX", "alert text")

    def test_send_success_no_enqueue(self, monkeypatch):
        monkeypatch.setattr(worker_monitor.settings, "daily_report_chat_id", "GroupX")

        with patch("app.wecom.client.WeComClient") as mock_client_cls, \
             patch("app.tasks.send_retry_drain.enqueue_group_send_retry") as mock_enqueue:
            client_inst = MagicMock()
            client_inst.send_text_to_group.return_value = True
            mock_client_cls.return_value = client_inst

            worker_monitor._push_group("ok")

            mock_enqueue.assert_not_called()
