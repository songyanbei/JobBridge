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


# ---------------------------------------------------------------------------
# Bug 1 — search 路径的 missing_fields 合并后复核
# ---------------------------------------------------------------------------

class TestIsFieldFilled:
    def test_missing_key_is_not_filled(self):
        assert message_router._is_field_filled({}, "city") is False

    def test_none_is_not_filled(self):
        assert message_router._is_field_filled({"city": None}, "city") is False

    def test_empty_list_is_not_filled(self):
        assert message_router._is_field_filled({"city": []}, "city") is False

    def test_empty_string_is_not_filled(self):
        assert message_router._is_field_filled({"city": ""}, "city") is False

    def test_zero_is_filled(self):
        # salary_floor_monthly=0 是合法值，不能算缺失
        assert message_router._is_field_filled({"salary_floor_monthly": 0}, "salary_floor_monthly") is True

    def test_false_is_filled(self):
        # provide_meal=False 是合法值
        assert message_router._is_field_filled({"provide_meal": False}, "provide_meal") is True

    def test_non_empty_list_is_filled(self):
        assert message_router._is_field_filled({"city": ["北京市"]}, "city") is True


class TestComputeSearchMissing:
    """Bug 1：合并后必须按 session.search_criteria 复核 LLM 给的 missing_fields。"""

    def test_llm_missing_filtered_against_session(self):
        # 用户先搜了"北京·餐饮·≥2200"，session 已有；现在说"西安有吗"，
        # LLM 错误地把 job_category 标进 missing。
        session = SessionState(
            role="worker",
            search_criteria={
                "city": ["西安市"],
                "job_category": ["餐饮"],
                "salary_floor_monthly": 2200,
            },
        )
        intent = IntentResult(
            intent="search_job",
            structured_data={"city": ["西安市"]},
            missing_fields=["job_category"],
            confidence=0.7,
        )
        assert message_router._compute_search_missing(intent, session) == []

    def test_empty_missing_returns_empty(self):
        # 空 session + LLM 报 missing=[] → 不在这里兜底，下游 has_effective_search_criteria
        # 兜（worker 简历默认条件需要在 _apply_default_criteria 里注入，Stage B P1-1）
        session = SessionState(role="worker", search_criteria={})
        intent = IntentResult(
            intent="search_job",
            structured_data={},
            missing_fields=[],
            confidence=0.3,
        )
        assert message_router._compute_search_missing(intent, session) == []

    def test_partial_session_keeps_unfilled_llm_missing(self):
        # session 只有 city，LLM 报 missing=[job_category] → 保留 job_category（未填）
        session = SessionState(
            role="worker",
            search_criteria={"city": ["北京市"]},
        )
        intent = IntentResult(
            intent="search_job",
            structured_data={},
            missing_fields=["job_category"],
            confidence=0.5,
        )
        assert message_router._compute_search_missing(intent, session) == ["job_category"]

    def test_llm_extra_missing_field_kept_when_session_empty(self):
        # LLM 主动追问 salary_floor_monthly（不在 min_required），session 没填 → 保留
        session = SessionState(
            role="worker",
            search_criteria={"city": ["北京市"], "job_category": ["餐饮"]},
        )
        intent = IntentResult(
            intent="search_job",
            structured_data={},
            missing_fields=["salary_floor_monthly"],
            confidence=0.6,
        )
        assert message_router._compute_search_missing(intent, session) == ["salary_floor_monthly"]

    def test_llm_extra_missing_field_dropped_when_session_has_it(self):
        # 同上，但 session 已有 salary → 不再问
        session = SessionState(
            role="worker",
            search_criteria={
                "city": ["北京市"],
                "job_category": ["餐饮"],
                "salary_floor_monthly": 3000,
            },
        )
        intent = IntentResult(
            intent="search_job",
            structured_data={},
            missing_fields=["salary_floor_monthly"],
            confidence=0.6,
        )
        assert message_router._compute_search_missing(intent, session) == []

    def test_dedupes_repeated_llm_missing(self):
        session = SessionState(role="worker", search_criteria={})
        intent = IntentResult(
            intent="search_job",
            structured_data={},
            missing_fields=["city", "city", "job_category"],
            confidence=0.4,
        )
        result = message_router._compute_search_missing(intent, session)
        assert result == ["city", "job_category"]

    def test_preserves_llm_order(self):
        session = SessionState(role="worker", search_criteria={})
        intent = IntentResult(
            intent="search_job",
            structured_data={},
            missing_fields=["salary_floor_monthly", "city"],
            confidence=0.4,
        )
        assert message_router._compute_search_missing(intent, session) == [
            "salary_floor_monthly",
            "city",
        ]

    def test_session_with_partial_complete_filters_all_llm_missing(self):
        # Bug 1 复刻 turn 12："饭店服务员"单字段消息，session 已有 city
        # → LLM 错误把 city 标 missing（短文本 hallucinate），应被过滤
        session = SessionState(
            role="worker",
            search_criteria={"city": ["西安市"], "job_category": ["餐饮"]},
        )
        intent = IntentResult(
            intent="search_job",
            structured_data={"job_category": ["餐饮"]},
            missing_fields=["city", "salary_floor_monthly"],
            confidence=0.5,
        )
        # city 在 session → 剔除；salary 不在 session 也不在 min → 保留
        assert message_router._compute_search_missing(intent, session) == [
            "salary_floor_monthly",
        ]


class TestSearchMissingRecheckIntegration:
    """通过 process() 端到端验证 Bug 1 修复在分发链路上生效。"""

    def _setup(self, mock_id, mock_check, mock_active, mock_load, session):
        mock_id.return_value = _ctx()
        mock_check.return_value = None
        mock_load.return_value = session

    @patch("app.services.message_router.user_service.update_last_active")
    @patch("app.services.message_router.user_service.check_user_status")
    @patch("app.services.message_router.user_service.identify_or_register")
    @patch("app.services.message_router.conversation_service.load_session")
    @patch("app.services.message_router.conversation_service.save_session")
    @patch("app.services.message_router.classify_intent")
    @patch("app.services.message_router.search_service.search_jobs")
    def test_session_has_category_llm_says_missing_still_searches(
        self, mock_search, mock_classify, mock_save, mock_load,
        mock_id, mock_check, mock_active,
    ):
        # 复刻真实 bug：session 已有 job_category，LLM 仍标它 missing → 应该直接走搜索
        session = SessionState(
            role="worker",
            search_criteria={
                "city": ["北京市"],
                "job_category": ["餐饮"],
                "salary_floor_monthly": 2200,
            },
        )
        self._setup(mock_id, mock_check, mock_active, mock_load, session)
        mock_classify.return_value = IntentResult(
            intent="search_job",
            structured_data={"city": ["西安市"]},
            missing_fields=["job_category"],
            confidence=0.7,
        )
        sr = MagicMock()
        sr.reply_text = "1 个岗位"
        mock_search.return_value = sr

        replies = process(_msg(content="西安有吗"), MagicMock())

        assert replies[0].content == "1 个岗位"
        mock_search.assert_called_once()

    @patch("app.services.message_router.user_service.update_last_active")
    @patch("app.services.message_router.user_service.check_user_status")
    @patch("app.services.message_router.user_service.identify_or_register")
    @patch("app.services.message_router.conversation_service.load_session")
    @patch("app.services.message_router.conversation_service.save_session")
    @patch("app.services.message_router.classify_intent")
    @patch("app.services.message_router.search_service.search_jobs")
    def test_partial_recheck_keeps_truly_missing(
        self, mock_search, mock_classify, mock_save, mock_load,
        mock_id, mock_check, mock_active,
    ):
        # session 已有 city，LLM 报 missing=[city, job_category]：
        # city 应被过滤，job_category 保留 → 仍追问 job_category
        session = SessionState(
            role="worker",
            search_criteria={"city": ["北京市"]},
        )
        self._setup(mock_id, mock_check, mock_active, mock_load, session)
        mock_classify.return_value = IntentResult(
            intent="search_job",
            structured_data={},
            missing_fields=["city", "job_category"],
            confidence=0.5,
        )

        replies = process(_msg(content="想找个活"), MagicMock())

        assert "信息还不够完整" in replies[0].content
        assert "工种" in replies[0].content
        # 工作城市不应再问（session 已有"北京市"）
        assert "工作城市" not in replies[0].content
        mock_search.assert_not_called()
