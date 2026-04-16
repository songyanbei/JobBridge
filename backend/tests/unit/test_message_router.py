"""message_router 单元测试（Phase 4）。"""
from unittest.mock import MagicMock, patch

import pytest

from app.llm.base import IntentResult
from app.schemas.conversation import ReplyMessage, SessionState
from app.services import message_router
from app.services.message_router import (
    BLOCKED_REPLY,
    DELETED_REPLY,
    FALLBACK_REPLY,
    FILE_NOT_SUPPORTED,
    IMAGE_DOWNLOAD_FAILED,
    IMAGE_RECEIVED_NON_UPLOAD,
    SYSTEM_BUSY_REPLY,
    UNKNOWN_TYPE_REPLY,
    VOICE_NOT_SUPPORTED,
    _build_welcome,
    process,
)
from app.services.user_service import UserContext
from app.wecom.callback import WeComMessage


def _ctx(role="worker", should_welcome=False, status="active"):
    return UserContext(
        external_userid="u1", role=role, status=status,
        display_name="张三", company="X厂" if role == "factory" else None,
        contact_person="张三" if role == "factory" else None,
        phone=None,
        can_search_jobs=role in ("worker", "broker"),
        can_search_workers=role in ("factory", "broker"),
        is_first_touch=should_welcome, should_welcome=should_welcome,
    )


def _msg(msg_type="text", content="你好", from_user="u1", media_id="", image_url=""):
    return WeComMessage(
        msg_id="m1", from_user=from_user, to_user="bot",
        msg_type=msg_type, content=content, media_id=media_id,
        image_url=image_url, create_time=1700000000,
    )


# ---------------------------------------------------------------------------
# 状态拦截
# ---------------------------------------------------------------------------

class TestStatusInterception:
    @patch("app.services.message_router.user_service.identify_or_register")
    def test_blocked_user_short_circuits(self, mock_id):
        mock_id.return_value = _ctx(status="blocked")
        replies = process(_msg(), MagicMock())
        assert len(replies) == 1
        assert replies[0].content == BLOCKED_REPLY

    @patch("app.services.message_router.user_service.identify_or_register")
    def test_deleted_user_short_circuits(self, mock_id):
        mock_id.return_value = _ctx(status="deleted")
        replies = process(_msg(), MagicMock())
        assert len(replies) == 1
        assert replies[0].content == DELETED_REPLY

    def test_empty_from_user_returns_empty(self):
        replies = process(_msg(from_user=""), MagicMock())
        assert replies == []


# ---------------------------------------------------------------------------
# 消息类型分流
# ---------------------------------------------------------------------------

class TestTypeDispatch:
    def _stub_user(self, mock_id, mock_check, mock_active):
        mock_id.return_value = _ctx()
        mock_check.return_value = None

    @patch("app.services.message_router.user_service.update_last_active")
    @patch("app.services.message_router.user_service.check_user_status")
    @patch("app.services.message_router.user_service.identify_or_register")
    def test_voice_rejected(self, mock_id, mock_check, mock_active):
        self._stub_user(mock_id, mock_check, mock_active)
        replies = process(_msg(msg_type="voice"), MagicMock())
        assert replies[0].content == VOICE_NOT_SUPPORTED

    @patch("app.services.message_router.user_service.update_last_active")
    @patch("app.services.message_router.user_service.check_user_status")
    @patch("app.services.message_router.user_service.identify_or_register")
    def test_file_rejected(self, mock_id, mock_check, mock_active):
        self._stub_user(mock_id, mock_check, mock_active)
        replies = process(_msg(msg_type="file"), MagicMock())
        assert replies[0].content == FILE_NOT_SUPPORTED

    @patch("app.services.message_router.user_service.update_last_active")
    @patch("app.services.message_router.user_service.check_user_status")
    @patch("app.services.message_router.user_service.identify_or_register")
    def test_event_returns_empty(self, mock_id, mock_check, mock_active):
        self._stub_user(mock_id, mock_check, mock_active)
        replies = process(_msg(msg_type="event", content="subscribe"), MagicMock())
        assert replies == []

    @patch("app.services.message_router.user_service.update_last_active")
    @patch("app.services.message_router.user_service.check_user_status")
    @patch("app.services.message_router.user_service.identify_or_register")
    def test_unknown_type_fallback(self, mock_id, mock_check, mock_active):
        self._stub_user(mock_id, mock_check, mock_active)
        replies = process(_msg(msg_type="weird_type"), MagicMock())
        assert replies[0].content == UNKNOWN_TYPE_REPLY


# ---------------------------------------------------------------------------
# 图片消息
# ---------------------------------------------------------------------------

class TestImageHandling:
    @patch("app.services.message_router.user_service.update_last_active")
    @patch("app.services.message_router.user_service.check_user_status")
    @patch("app.services.message_router.user_service.identify_or_register")
    @patch("app.services.message_router.conversation_service.load_session")
    def test_image_without_url_returns_failure(
        self, mock_load, mock_id, mock_check, mock_active,
    ):
        mock_id.return_value = _ctx()
        mock_check.return_value = None
        replies = process(_msg(msg_type="image", image_url=""), MagicMock())
        assert replies[0].content == IMAGE_DOWNLOAD_FAILED

    @patch("app.services.message_router.user_service.update_last_active")
    @patch("app.services.message_router.user_service.check_user_status")
    @patch("app.services.message_router.user_service.identify_or_register")
    @patch("app.services.message_router.conversation_service.load_session")
    def test_image_non_upload_flow_returns_received(
        self, mock_load, mock_id, mock_check, mock_active,
    ):
        mock_id.return_value = _ctx()
        mock_check.return_value = None
        mock_load.return_value = None
        replies = process(
            _msg(msg_type="image", image_url="/files/img/x.jpg"), MagicMock(),
        )
        assert replies[0].content == IMAGE_RECEIVED_NON_UPLOAD

    @patch("app.services.message_router.user_service.update_last_active")
    @patch("app.services.message_router.user_service.check_user_status")
    @patch("app.services.message_router.user_service.identify_or_register")
    @patch("app.services.message_router.conversation_service.load_session")
    @patch("app.services.message_router.conversation_service.save_session")
    @patch("app.services.message_router.upload_service.attach_image")
    def test_image_in_upload_flow_calls_attach(
        self, mock_attach, mock_save, mock_load, mock_id, mock_check, mock_active,
    ):
        mock_id.return_value = _ctx()
        mock_check.return_value = None
        session = SessionState(role="worker", current_intent="upload_resume")
        mock_load.return_value = session
        mock_attach.return_value = "图片已附加"
        replies = process(
            _msg(msg_type="image", image_url="/files/img/x.jpg"), MagicMock(),
        )
        assert replies[0].content == "图片已附加"
        mock_attach.assert_called_once()


# ---------------------------------------------------------------------------
# 文本链路 — 首次欢迎
# ---------------------------------------------------------------------------

class TestWelcome:
    @patch("app.services.message_router.user_service.update_last_active")
    @patch("app.services.message_router.user_service.check_user_status")
    @patch("app.services.message_router.user_service.identify_or_register")
    @patch("app.services.message_router.conversation_service.load_session")
    @patch("app.services.message_router.conversation_service.save_session")
    @patch("app.services.message_router.classify_intent")
    def test_should_welcome_returns_welcome_without_classify(
        self, mock_classify, mock_save, mock_load,
        mock_id, mock_check, mock_active,
    ):
        mock_id.return_value = _ctx(should_welcome=True)
        mock_check.return_value = None
        mock_load.return_value = None

        replies = process(_msg(), MagicMock())

        assert len(replies) == 1
        assert "JobBridge" in replies[0].content
        mock_classify.assert_not_called()


# ---------------------------------------------------------------------------
# 文本链路 — 意图分发
# ---------------------------------------------------------------------------

class TestIntentDispatch:
    def _setup(self, mock_id, mock_check, mock_active, mock_load, session=None):
        mock_id.return_value = _ctx()
        mock_check.return_value = None
        mock_load.return_value = session or SessionState(role="worker")

    @patch("app.services.message_router.user_service.update_last_active")
    @patch("app.services.message_router.user_service.check_user_status")
    @patch("app.services.message_router.user_service.identify_or_register")
    @patch("app.services.message_router.conversation_service.load_session")
    @patch("app.services.message_router.conversation_service.save_session")
    @patch("app.services.message_router.classify_intent")
    @patch("app.services.message_router.command_service.execute")
    def test_command_intent_delegates_to_command_service(
        self, mock_cmd, mock_classify, mock_save, mock_load,
        mock_id, mock_check, mock_active,
    ):
        self._setup(mock_id, mock_check, mock_active, mock_load)
        mock_classify.return_value = IntentResult(
            intent="command",
            structured_data={"command": "help", "args": ""},
            confidence=1.0,
        )
        mock_cmd.return_value = [ReplyMessage(userid="u1", content="help-reply")]

        replies = process(_msg(content="/帮助"), MagicMock())
        assert replies[0].content == "help-reply"
        mock_cmd.assert_called_once()
        args, _kwargs = mock_cmd.call_args
        assert args[0] == "help"
        assert args[1] == ""

    @patch("app.services.message_router.user_service.update_last_active")
    @patch("app.services.message_router.user_service.check_user_status")
    @patch("app.services.message_router.user_service.identify_or_register")
    @patch("app.services.message_router.conversation_service.load_session")
    @patch("app.services.message_router.conversation_service.save_session")
    @patch("app.services.message_router.classify_intent")
    @patch("app.services.message_router.search_service.search_jobs")
    def test_search_job_intent_invokes_search_service(
        self, mock_search, mock_classify, mock_save, mock_load,
        mock_id, mock_check, mock_active,
    ):
        self._setup(mock_id, mock_check, mock_active, mock_load)
        mock_classify.return_value = IntentResult(
            intent="search_job",
            structured_data={"city": ["苏州市"], "job_category": ["电子厂"]},
            missing_fields=[],
            confidence=0.9,
        )
        search_result = MagicMock()
        search_result.reply_text = "3 个岗位"
        mock_search.return_value = search_result

        replies = process(_msg(content="苏州找电子厂"), MagicMock())
        assert replies[0].content == "3 个岗位"
        mock_search.assert_called_once()

    @patch("app.services.message_router.user_service.update_last_active")
    @patch("app.services.message_router.user_service.check_user_status")
    @patch("app.services.message_router.user_service.identify_or_register")
    @patch("app.services.message_router.conversation_service.load_session")
    @patch("app.services.message_router.conversation_service.save_session")
    @patch("app.services.message_router.classify_intent")
    def test_chitchat_returns_guidance(
        self, mock_classify, mock_save, mock_load,
        mock_id, mock_check, mock_active,
    ):
        self._setup(mock_id, mock_check, mock_active, mock_load)
        mock_classify.return_value = IntentResult(
            intent="chitchat", confidence=0.7,
        )
        replies = process(_msg(content="你好"), MagicMock())
        assert "您好" in replies[0].content

    @patch("app.services.message_router.user_service.update_last_active")
    @patch("app.services.message_router.user_service.check_user_status")
    @patch("app.services.message_router.user_service.identify_or_register")
    @patch("app.services.message_router.conversation_service.load_session")
    @patch("app.services.message_router.conversation_service.save_session")
    @patch("app.services.message_router.classify_intent")
    def test_classify_exception_returns_system_busy(
        self, mock_classify, mock_save, mock_load,
        mock_id, mock_check, mock_active,
    ):
        self._setup(mock_id, mock_check, mock_active, mock_load)
        mock_classify.side_effect = RuntimeError("llm timeout")
        replies = process(_msg(content="你好"), MagicMock())
        assert replies[0].content == SYSTEM_BUSY_REPLY

    @patch("app.services.message_router.user_service.update_last_active")
    @patch("app.services.message_router.user_service.check_user_status")
    @patch("app.services.message_router.user_service.identify_or_register")
    @patch("app.services.message_router.conversation_service.load_session")
    @patch("app.services.message_router.conversation_service.save_session")
    @patch("app.services.message_router.classify_intent")
    @patch("app.services.message_router.search_service.show_more")
    def test_show_more_intent_calls_show_more(
        self, mock_show, mock_classify, mock_save, mock_load,
        mock_id, mock_check, mock_active,
    ):
        self._setup(mock_id, mock_check, mock_active, mock_load)
        mock_classify.return_value = IntentResult(intent="show_more", confidence=1.0)
        sr = MagicMock()
        sr.reply_text = "更多结果"
        mock_show.return_value = sr

        replies = process(_msg(content="更多"), MagicMock())
        assert replies[0].content == "更多结果"


# ---------------------------------------------------------------------------
# 欢迎语构造
# ---------------------------------------------------------------------------

class TestSearchDirectionResolution:
    """P1-4：broker 无方向时应尊重 intent，并隐式写 session.broker_direction。"""

    @patch("app.services.message_router.user_service.update_last_active")
    @patch("app.services.message_router.user_service.check_user_status")
    @patch("app.services.message_router.user_service.identify_or_register")
    @patch("app.services.message_router.conversation_service.load_session")
    @patch("app.services.message_router.conversation_service.save_session")
    @patch("app.services.message_router.classify_intent")
    @patch("app.services.message_router.search_service.search_jobs")
    def test_broker_search_job_without_direction_calls_search_jobs(
        self, mock_search_jobs, mock_classify, mock_save, mock_load,
        mock_id, mock_check, mock_active,
    ):
        # broker 用户，session.broker_direction 未设置
        mock_id.return_value = _ctx(role="broker")
        mock_check.return_value = None
        session = SessionState(role="broker", broker_direction=None)
        mock_load.return_value = session
        mock_classify.return_value = IntentResult(
            intent="search_job",
            structured_data={"city": ["苏州市"], "job_category": ["电子厂"]},
            missing_fields=[],
            confidence=0.9,
        )
        sr = MagicMock()
        sr.reply_text = "3 个岗位"
        mock_search_jobs.return_value = sr

        replies = process(_msg(content="帮工人找苏州电子厂"), MagicMock())

        assert replies[0].content == "3 个岗位"
        mock_search_jobs.assert_called_once()
        # 同步写入 broker_direction，供后续 follow_up / show_more 使用
        assert session.broker_direction == "search_job"

    @patch("app.services.message_router.user_service.update_last_active")
    @patch("app.services.message_router.user_service.check_user_status")
    @patch("app.services.message_router.user_service.identify_or_register")
    @patch("app.services.message_router.conversation_service.load_session")
    @patch("app.services.message_router.conversation_service.save_session")
    @patch("app.services.message_router.classify_intent")
    @patch("app.services.message_router.search_service.search_workers")
    def test_broker_search_worker_intent_calls_search_workers(
        self, mock_search_workers, mock_classify, mock_save, mock_load,
        mock_id, mock_check, mock_active,
    ):
        mock_id.return_value = _ctx(role="broker")
        mock_check.return_value = None
        session = SessionState(role="broker", broker_direction=None)
        mock_load.return_value = session
        mock_classify.return_value = IntentResult(
            intent="search_worker",
            structured_data={"city": ["苏州市"]},
            missing_fields=[],
            confidence=0.9,
        )
        sr = MagicMock()
        sr.reply_text = "3 位求职者"
        mock_search_workers.return_value = sr

        replies = process(_msg(content="苏州招电子厂工人"), MagicMock())

        assert replies[0].content == "3 位求职者"
        mock_search_workers.assert_called_once()
        assert session.broker_direction == "search_worker"


class TestBuildWelcome:
    def test_worker_welcome(self):
        text = _build_welcome(_ctx(role="worker"))
        assert "JobBridge" in text

    def test_factory_welcome_with_company(self):
        text = _build_welcome(_ctx(role="factory"))
        assert "X厂" in text

    def test_broker_welcome_with_name(self):
        text = _build_welcome(_ctx(role="broker"))
        assert "中介" in text
