"""command_service 单元测试（Phase 4）。"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.schemas.conversation import ReplyMessage, SessionState, CandidateSnapshot
from app.services import command_service
from app.services.command_service import (
    HELP_TEXT,
    HUMAN_AGENT_TEXT,
    BROKER_ONLY,
    ROLE_NOT_ALLOWED,
    NO_RENEWABLE_JOB,
    NO_DELISTABLE_JOB,
    NO_FILLABLE_JOB,
    RESET_SEARCH_EMPTY,
    RESET_SEARCH_SUCCESS,
    SWITCH_JOB_OK,
    SWITCH_WORKER_OK,
    _parse_renew_days,
    execute,
)
from app.services.user_service import UserContext


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_user_ctx(role="worker", userid="u1", display_name="张三"):
    return UserContext(
        external_userid=userid, role=role, status="active",
        display_name=display_name, company="X厂" if role == "factory" else None,
        contact_person=None, phone=None,
        can_search_jobs=(role in ("worker", "broker")),
        can_search_workers=(role in ("factory", "broker")),
        is_first_touch=False, should_welcome=False,
    )


def _make_session(role="worker", **kwargs):
    return SessionState(role=role, **kwargs)


def _make_job_mock(job_id=1, city="苏州", category="电子厂",
                   expires_at=None, created_at=None):
    now = datetime.now(timezone.utc)
    job = MagicMock()
    job.id = job_id
    job.city = city
    job.job_category = category
    job.expires_at = expires_at or (now + timedelta(days=10))
    job.created_at = created_at or now
    job.delist_reason = None
    return job


# ---------------------------------------------------------------------------
# /帮助
# ---------------------------------------------------------------------------

class TestHelp:
    def test_returns_help_text(self):
        replies = execute("help", "", _make_user_ctx(), None, MagicMock())
        assert len(replies) == 1
        assert replies[0].content == HELP_TEXT
        assert replies[0].userid == "u1"


class TestHumanAgent:
    def test_returns_human_agent_text(self):
        replies = execute("human_agent", "", _make_user_ctx(), None, MagicMock())
        assert len(replies) == 1
        assert replies[0].content == HUMAN_AGENT_TEXT


# ---------------------------------------------------------------------------
# /重新找
# ---------------------------------------------------------------------------

class TestResetSearch:
    def test_empty_session_returns_empty_hint(self):
        replies = execute("reset_search", "", _make_user_ctx(), None, MagicMock())
        assert replies[0].content == RESET_SEARCH_EMPTY

    def test_session_without_state_returns_empty_hint(self):
        session = _make_session()
        replies = execute("reset_search", "", _make_user_ctx(), session, MagicMock())
        assert replies[0].content == RESET_SEARCH_EMPTY

    @patch("app.services.command_service.conversation_service.save_session")
    @patch("app.services.command_service.conversation_service.reset_search")
    def test_session_with_criteria_clears_and_replies_success(self, mock_reset, mock_save):
        session = _make_session(search_criteria={"city": ["苏州"]})
        replies = execute("reset_search", "", _make_user_ctx(), session, MagicMock())
        assert replies[0].content == RESET_SEARCH_SUCCESS
        mock_reset.assert_called_once()
        mock_save.assert_called_once()


# ---------------------------------------------------------------------------
# /找岗位 /找工人
# ---------------------------------------------------------------------------

class TestSwitchDirection:
    def test_non_broker_rejected_for_search_job(self):
        replies = execute(
            "switch_to_job", "", _make_user_ctx(role="worker"), None, MagicMock(),
        )
        assert replies[0].content == BROKER_ONLY

    def test_non_broker_rejected_for_search_worker(self):
        replies = execute(
            "switch_to_worker", "", _make_user_ctx(role="factory"), None, MagicMock(),
        )
        assert replies[0].content == BROKER_ONLY

    @patch("app.services.command_service.conversation_service.save_session")
    def test_broker_switch_to_job_ok(self, mock_save):
        session = _make_session(role="broker")
        replies = execute(
            "switch_to_job", "", _make_user_ctx(role="broker"), session, MagicMock(),
        )
        assert replies[0].content == SWITCH_JOB_OK
        assert session.broker_direction == "search_job"
        mock_save.assert_called_once()

    @patch("app.services.command_service.conversation_service.save_session")
    def test_broker_switch_to_worker_ok(self, mock_save):
        session = _make_session(role="broker")
        replies = execute(
            "switch_to_worker", "", _make_user_ctx(role="broker"), session, MagicMock(),
        )
        assert replies[0].content == SWITCH_WORKER_OK
        assert session.broker_direction == "search_worker"
        mock_save.assert_called_once()


# ---------------------------------------------------------------------------
# /我的状态
# ---------------------------------------------------------------------------

class TestMyStatus:
    @patch("app.services.command_service.get_user_status")
    def test_user_not_found(self, mock_get_status):
        mock_get_status.return_value = {"found": False, "message": "未找到您的账号记录。"}
        replies = execute("my_status", "", _make_user_ctx(), None, MagicMock())
        assert "未找到您的账号记录" in replies[0].content

    @patch("app.services.command_service.get_user_status")
    def test_user_with_latest_job(self, mock_get_status):
        mock_get_status.return_value = {
            "found": True, "role": "factory", "status": "active",
            "registered_at": "2026-04-01",
            "latest_job": {"id": 1, "audit_status": "passed", "created_at": "2026-04-10"},
        }
        replies = execute("my_status", "", _make_user_ctx(role="factory"), None, MagicMock())
        assert "正常" in replies[0].content
        assert "#1" in replies[0].content


# ---------------------------------------------------------------------------
# /删除我的信息
# ---------------------------------------------------------------------------

class TestDeleteMyData:
    def test_non_worker_rejected(self):
        replies = execute(
            "delete_my_data", "", _make_user_ctx(role="factory"), None, MagicMock(),
        )
        assert "仅对工人账号开放" in replies[0].content

    @patch("app.services.command_service.delete_user_data")
    def test_worker_calls_user_service(self, mock_delete):
        mock_delete.return_value = "已收到删除请求"
        replies = execute(
            "delete_my_data", "", _make_user_ctx(role="worker"), None, MagicMock(),
        )
        assert replies[0].content == "已收到删除请求"
        mock_delete.assert_called_once()


# ---------------------------------------------------------------------------
# /续期
# ---------------------------------------------------------------------------

class TestParseRenewDays:
    def test_empty_defaults_to_15(self):
        assert _parse_renew_days("") == 15

    def test_accepts_15(self):
        assert _parse_renew_days("15") == 15
        assert _parse_renew_days("15天") == 15

    def test_accepts_30(self):
        assert _parse_renew_days("30") == 30

    def test_rejects_other_numbers(self):
        assert _parse_renew_days("7") is None
        assert _parse_renew_days("60") is None

    def test_rejects_non_numeric(self):
        assert _parse_renew_days("abc") is None


class TestRenewJob:
    def test_non_factory_role_rejected(self):
        replies = execute(
            "renew_job", "", _make_user_ctx(role="worker"), None, MagicMock(),
        )
        assert replies[0].content == ROLE_NOT_ALLOWED

    def test_invalid_days_rejected(self):
        db = MagicMock()
        replies = execute(
            "renew_job", "7", _make_user_ctx(role="factory"), None, db,
        )
        assert "15 或 30" in replies[0].content

    def test_no_jobs_returns_empty_hint(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []
        replies = execute(
            "renew_job", "", _make_user_ctx(role="factory"), None, db,
        )
        assert replies[0].content == NO_RENEWABLE_JOB

    def test_single_job_renewed_by_default_15_days(self):
        db = MagicMock()
        now = datetime.now(timezone.utc)
        job = _make_job_mock(
            job_id=10, city="苏州", category="电子厂",
            expires_at=now + timedelta(days=5),
            created_at=now - timedelta(days=5),
        )
        # 第一次查询返回 [job]，第二次查询（ttl cap）返回 SystemConfig
        db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [job]
        cfg = MagicMock()
        cfg.config_value = "30"
        db.query.return_value.filter.return_value.first.return_value = cfg

        replies = execute(
            "renew_job", "", _make_user_ctx(role="factory"), None, db,
        )
        assert "续期 15 天" in replies[0].content
        assert "#10" in replies[0].content

    def test_multiple_jobs_with_explicit_days_renews_latest(self):
        db = MagicMock()
        now = datetime.now(timezone.utc)
        job1 = _make_job_mock(job_id=1, expires_at=now + timedelta(days=3))
        job2 = _make_job_mock(job_id=2, expires_at=now + timedelta(days=10))
        db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [job1, job2]
        cfg = MagicMock()
        cfg.config_value = "30"
        db.query.return_value.filter.return_value.first.return_value = cfg

        replies = execute(
            "renew_job", "15", _make_user_ctx(role="factory"), None, db,
        )
        assert "#1" in replies[0].content
        assert "还有 1 个在线岗位" in replies[0].content

    def test_multiple_jobs_without_args_returns_list(self):
        """phase4-main §3.1 D：多岗位且无参数时应返回列表让用户确认。"""
        db = MagicMock()
        now = datetime.now(timezone.utc)
        job1 = _make_job_mock(job_id=1, expires_at=now + timedelta(days=3))
        job2 = _make_job_mock(job_id=2, expires_at=now + timedelta(days=10))
        job3 = _make_job_mock(job_id=3, expires_at=now + timedelta(days=7))
        db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [job1, job2, job3]

        replies = execute(
            "renew_job", "", _make_user_ctx(role="factory"), None, db,
        )
        content = replies[0].content
        assert "多个可续期" in content
        # 列表条目
        assert "#1" in content
        assert "#2" in content
        assert "#3" in content
        # 没有真的执行续期：expires_at 应保持原值
        assert job1.expires_at == now + timedelta(days=3)
        # 提示用户如何确认
        assert "/续期" in content

    def test_ttl_cap_does_not_shrink_existing_expires(self):
        """老岗位已续期多次时，新 cap 不应回退到比 current 更小的值。"""
        db = MagicMock()
        now = datetime.now(timezone.utc)
        # 岗位已经续期到 now+50 天
        job = _make_job_mock(
            job_id=10,
            expires_at=now + timedelta(days=50),
            created_at=now - timedelta(days=80),
        )
        db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [job]
        cfg = MagicMock()
        cfg.config_value = "30"  # ttl.job.days=30 → cap=60 天
        db.query.return_value.filter.return_value.first.return_value = cfg

        replies = execute(
            "renew_job", "30", _make_user_ctx(role="factory"), None, db,
        )
        # 续期 30 天 → now+80，cap=now+60，应截断但不回退到 < now+50
        # max(cap=now+60, current=now+50) = now+60
        assert job.expires_at >= now + timedelta(days=50)
        assert "续期 30 天" in replies[0].content


class TestDelistJob:
    def test_non_factory_role_rejected(self):
        replies = execute(
            "delist_job", "", _make_user_ctx(role="worker"), None, MagicMock(),
        )
        assert replies[0].content == ROLE_NOT_ALLOWED

    def test_no_jobs_returns_empty_hint(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []
        replies = execute(
            "delist_job", "", _make_user_ctx(role="factory"), None, db,
        )
        assert replies[0].content == NO_DELISTABLE_JOB

    def test_delist_sets_reason_manual(self):
        db = MagicMock()
        job = _make_job_mock(job_id=7)
        db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [job]
        replies = execute(
            "delist_job", "", _make_user_ctx(role="factory"), None, db,
        )
        assert job.delist_reason == "manual_delist"
        assert "下架" in replies[0].content


class TestFilledJob:
    def test_no_jobs_returns_empty_hint(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []
        replies = execute(
            "filled_job", "", _make_user_ctx(role="broker"), None, db,
        )
        assert replies[0].content == NO_FILLABLE_JOB

    def test_filled_sets_reason_filled(self):
        db = MagicMock()
        job = _make_job_mock(job_id=9)
        db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [job]
        replies = execute(
            "filled_job", "", _make_user_ctx(role="factory"), None, db,
        )
        assert job.delist_reason == "filled"
        assert "招满" in replies[0].content


# ---------------------------------------------------------------------------
# unknown command
# ---------------------------------------------------------------------------

class TestUnknownCommand:
    def test_unknown_returns_fallback(self):
        replies = execute(
            "definitely_not_a_command", "", _make_user_ctx(), None, MagicMock(),
        )
        assert "暂不支持该指令" in replies[0].content
