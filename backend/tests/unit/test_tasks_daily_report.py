"""Phase 7 tasks/daily_report.py 单元测试。

验证：
- ``run()`` 未取到锁直接 return，不组装报文
- ``run()`` ``daily_report_chat_id`` 为空时只 log 不推送，不入重试队列
- ``run()`` 推送失败时调用 ``enqueue_group_send_retry``
- ``_compose_report`` 输出文本包含 §13.5 要求的关键标签 / 字段
- ``_audit_reject_rate`` 在无审核数据时返回 (0, 0)，不抛异常
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.tasks import daily_report


@pytest.fixture(autouse=True)
def _stub_redis(monkeypatch):
    """所有用例默认 mock 掉 redis：避免单测连真实 redis。"""
    fake = MagicMock()
    fake.llen.return_value = 0
    fake.scan_iter.return_value = iter([])
    monkeypatch.setattr(daily_report, "get_redis", lambda: fake)
    return fake


# ---------------------------------------------------------------------------
# run() 入口分支
# ---------------------------------------------------------------------------

class TestRunLock:
    def test_skip_when_lock_not_acquired(self):
        with patch.object(daily_report, "task_lock") as mock_lock, \
             patch.object(daily_report, "_compose_report") as mock_compose:
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=False)
            cm.__exit__ = MagicMock(return_value=False)
            mock_lock.return_value = cm

            daily_report.run()

            mock_compose.assert_not_called()


class TestRunChatIdEmpty:
    """daily_report_chat_id 未配置时只写 loguru，不实例化 WeComClient。"""

    def test_no_chat_id_skips_push(self, monkeypatch):
        monkeypatch.setattr(daily_report.settings, "daily_report_chat_id", "")

        with patch.object(daily_report, "task_lock") as mock_lock, \
             patch.object(daily_report, "_compose_report", return_value="report content"), \
             patch("app.wecom.client.WeComClient") as mock_client_cls, \
             patch.object(daily_report, "enqueue_group_send_retry") as mock_enqueue:
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=True)
            cm.__exit__ = MagicMock(return_value=False)
            mock_lock.return_value = cm

            daily_report.run()

            mock_client_cls.assert_not_called()
            mock_enqueue.assert_not_called()


class TestRunPushFailureEnqueuesRetry:
    """推送失败时报文进 queue:group_send_retry，不丢失。"""

    def test_push_fail_enqueues(self, monkeypatch):
        monkeypatch.setattr(daily_report.settings, "daily_report_chat_id", "GroupX")

        with patch.object(daily_report, "task_lock") as mock_lock, \
             patch.object(daily_report, "_compose_report", return_value="report"), \
             patch("app.wecom.client.WeComClient") as mock_client_cls, \
             patch.object(daily_report, "enqueue_group_send_retry") as mock_enqueue:
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=True)
            cm.__exit__ = MagicMock(return_value=False)
            mock_lock.return_value = cm

            client_inst = MagicMock()
            client_inst.send_text_to_group.return_value = False  # 推送失败
            mock_client_cls.return_value = client_inst

            daily_report.run()

            client_inst.send_text_to_group.assert_called_once_with("GroupX", "report")
            mock_enqueue.assert_called_once_with("GroupX", "report")

    def test_push_success_does_not_enqueue(self, monkeypatch):
        monkeypatch.setattr(daily_report.settings, "daily_report_chat_id", "GroupX")

        with patch.object(daily_report, "task_lock") as mock_lock, \
             patch.object(daily_report, "_compose_report", return_value="report"), \
             patch("app.wecom.client.WeComClient") as mock_client_cls, \
             patch.object(daily_report, "enqueue_group_send_retry") as mock_enqueue:
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=True)
            cm.__exit__ = MagicMock(return_value=False)
            mock_lock.return_value = cm

            client_inst = MagicMock()
            client_inst.send_text_to_group.return_value = True
            mock_client_cls.return_value = client_inst

            daily_report.run()

            mock_enqueue.assert_not_called()


# ---------------------------------------------------------------------------
# _compose_report 文本结构
# ---------------------------------------------------------------------------

class TestComposeReportShape:
    """报文必须含 §13.5 / §17.1 要求的关键标签，否则运营群读起来不连贯。"""

    def test_contains_required_labels(self, monkeypatch):
        # mock dashboard
        fake_dashboard = {
            "today": {
                "dau_total": 100,
                "uploads_job": 5,
                "uploads_resume": 8,
                "search_count": 30,
                "hit_rate": 0.7,
                "empty_recall_rate": 0.1,
                "audit_pending": 2,
            },
            "yesterday": {
                "dau_total": 80,
                "search_count": 25,
                "hit_rate": 0.6,
            },
        }
        with patch.object(daily_report.report_service, "get_dashboard", return_value=fake_dashboard), \
             patch.object(daily_report, "SessionLocal") as mock_session, \
             patch.object(daily_report, "_audit_reject_rate", return_value=(1, 10)), \
             patch.object(daily_report, "_new_blocked_count", return_value=3):
            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=MagicMock())
            ctx.__exit__ = MagicMock(return_value=False)
            mock_session.return_value = ctx

            text = daily_report._compose_report()

        assert "JobBridge 日报" in text
        assert "DAU" in text
        assert "上传" in text
        assert "检索" in text
        assert "命中率" in text
        assert "空召回率" in text
        assert "审核打回率" in text
        assert "新增封禁" in text
        assert "待审积压" in text
        assert "Worker 健康" in text


# ---------------------------------------------------------------------------
# _audit_reject_rate 边界
# ---------------------------------------------------------------------------

class TestAuditRejectRate:
    def test_zero_when_no_rows(self):
        from datetime import date

        db = MagicMock()
        # query(...).filter(...).scalar() → 两次都 0
        db.query.return_value.filter.return_value.scalar.return_value = 0

        reject, total = daily_report._audit_reject_rate(db, date(2026, 1, 1))
        assert reject == 0
        assert total == 0
