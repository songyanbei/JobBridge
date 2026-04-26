"""Stage A 多轮上传守卫单测。

覆盖 docs/multi-turn-upload-stage-a-implementation.md §4 必测用例：
- SessionState 旧数据缺新字段也能反序列化
- process_upload missing 分支保存 pending
- 两轮上传：缺人数 -> 补 "2个人" -> 入库，不调用 search_workers
- /帮助 不清 pending
- "取消" 清 pending
- pending 超时后字段补丁文本提示重发
- _query_jobs/_query_resumes 在无 city/job_category 时返回空
- 入库 raw_text 包含两轮原文
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.llm.base import IntentResult
from app.schemas.conversation import SessionState
from app.services import message_router, upload_service
from app.services.message_router import (
    PENDING_CANCELLED_REPLY,
    PENDING_EXPIRED_REPLY,
    PENDING_MAX_ROUNDS_REPLY,
    process,
)
from app.services.search_service import (
    _query_jobs,
    _query_resumes,
    has_effective_search_criteria,
)
from app.services.user_service import UserContext
from app.wecom.callback import WeComMessage


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _ctx(role="factory"):
    return UserContext(
        external_userid="u1", role=role, status="active",
        display_name="张三", company="北京饭店" if role == "factory" else None,
        contact_person="张三" if role == "factory" else None,
        phone=None,
        can_search_jobs=role in ("worker", "broker"),
        can_search_workers=role in ("factory", "broker"),
        is_first_touch=False, should_welcome=False,
    )


def _msg(content: str):
    return WeComMessage(
        msg_id="m1", from_user="u1", to_user="bot",
        msg_type="text", content=content, media_id="",
        image_url="", create_time=1700000000,
    )


# ---------------------------------------------------------------------------
# Schema 兼容性
# ---------------------------------------------------------------------------

class TestSessionStateBackwardCompat:
    def test_legacy_session_dict_deserializes(self):
        """旧 Redis session 缺新字段时仍能构造。"""
        legacy = {
            "role": "factory",
            "current_intent": "upload_job",
            "search_criteria": {},
            "candidate_snapshot": None,
            "shown_items": [],
            "history": [],
            "updated_at": "",
            "broker_direction": None,
            "follow_up_rounds": 0,
        }
        s = SessionState(**legacy)
        assert s.pending_upload == {}
        assert s.pending_upload_intent is None
        assert s.awaiting_field is None
        assert s.pending_started_at is None
        assert s.pending_expires_at is None
        assert s.pending_raw_text_parts == []


# ---------------------------------------------------------------------------
# upload_service：缺字段时保存 pending
# ---------------------------------------------------------------------------

class TestProcessUploadSavesPending:
    @patch("app.services.upload_service.conversation_service")
    def test_missing_branch_saves_pending(self, mock_conv):
        user_ctx = _ctx("factory")
        intent = IntentResult(
            intent="upload_job",
            structured_data={
                "city": "北京市",
                "job_category": "餐饮",
                "salary_floor_monthly": 7500,
                "pay_type": "月薪",
            },  # 缺 headcount
            confidence=0.9,
        )
        session = SessionState(role="factory")
        db = MagicMock()

        result = upload_service.process_upload(
            user_ctx, intent, "北京饭店招聘厨师，底薪7500+绩效，包吃不包住",
            [], session, db,
        )

        assert result.success is False
        assert result.needs_followup is True
        # pending 已保存
        assert session.pending_upload_intent == "upload_job"
        assert session.pending_upload["city"] == "北京市"
        assert session.pending_upload["salary_floor_monthly"] == 7500
        assert session.awaiting_field == "headcount"
        assert session.pending_started_at is not None
        assert session.pending_expires_at is not None
        # 第一轮原文已入 parts
        assert session.pending_raw_text_parts == [
            "北京饭店招聘厨师，底薪7500+绩效，包吃不包住"
        ]


class TestClearPendingHelpers:
    def test_clear_pending_resets_all(self):
        s = SessionState(
            role="factory",
            pending_upload={"city": "北京市"},
            pending_upload_intent="upload_job",
            awaiting_field="headcount",
            pending_started_at="2026-04-26T10:00:00+00:00",
            pending_updated_at="2026-04-26T10:00:00+00:00",
            pending_expires_at="2026-04-26T10:10:00+00:00",
            pending_raw_text_parts=["a"],
            follow_up_rounds=2,
        )
        upload_service.clear_pending_upload(s)
        assert s.pending_upload == {}
        assert s.pending_upload_intent is None
        assert s.awaiting_field is None
        assert s.pending_started_at is None
        assert s.pending_expires_at is None
        assert s.pending_raw_text_parts == []
        assert s.follow_up_rounds == 0

    def test_is_pending_expired_false_when_no_pending(self):
        s = SessionState(role="factory")
        assert upload_service.is_pending_upload_expired(s) is False

    def test_is_pending_expired_true_when_past(self):
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        s = SessionState(role="factory", pending_expires_at=past)
        assert upload_service.is_pending_upload_expired(s) is True

    def test_is_pending_expired_false_when_future(self):
        future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        s = SessionState(role="factory", pending_expires_at=future)
        assert upload_service.is_pending_upload_expired(s) is False


# ---------------------------------------------------------------------------
# 搜索安全护栏
# ---------------------------------------------------------------------------

class TestSearchGuard:
    def test_has_effective_returns_false_for_headcount_only(self):
        assert has_effective_search_criteria({"headcount": 2}) is False

    def test_has_effective_returns_false_for_empty(self):
        assert has_effective_search_criteria({}) is False

    def test_has_effective_returns_true_for_city(self):
        assert has_effective_search_criteria({"city": ["北京市"]}) is True

    def test_has_effective_returns_true_for_job_category(self):
        assert has_effective_search_criteria({"job_category": ["餐饮"]}) is True

    def test_query_jobs_short_circuits_when_no_effective(self):
        db = MagicMock()
        result = _query_jobs({"headcount": 2}, 50, db)
        assert result == []
        db.query.assert_not_called()

    def test_query_resumes_short_circuits_when_no_effective(self):
        db = MagicMock()
        result = _query_resumes({"headcount": 2}, 50, db)
        assert result == []
        db.query.assert_not_called()


# ---------------------------------------------------------------------------
# 端到端：核心阻塞 bug
# ---------------------------------------------------------------------------

@pytest.fixture
def stub_user_pipeline():
    """Patch 用户识别 / 状态 / 活跃时间，并返回 factory 用户。"""
    with patch(
        "app.services.message_router.user_service.identify_or_register"
    ) as mock_id, patch(
        "app.services.message_router.user_service.check_user_status"
    ) as mock_check, patch(
        "app.services.message_router.user_service.update_last_active"
    ) as mock_active:
        mock_id.return_value = _ctx("factory")
        mock_check.return_value = None
        yield mock_id, mock_check, mock_active


class TestTwoTurnUpload:
    """场景 1：缺人数 -> '2个人' -> 入库；不调用 search_workers。"""

    @patch("app.services.message_router.conversation_service.save_session")
    @patch("app.services.message_router.conversation_service.load_session")
    @patch("app.services.message_router.classify_intent")
    @patch("app.services.upload_service.audit_service")
    @patch("app.services.upload_service._read_ttl_days")
    @patch("app.services.upload_service._create_job")
    @patch("app.services.message_router.search_service.search_workers")
    def test_patch_completes_upload_without_search(
        self,
        mock_search_workers,
        mock_create_job,
        mock_ttl,
        mock_audit,
        mock_classify,
        mock_load,
        mock_save,
        stub_user_pipeline,
    ):
        from app.services.audit_service import AuditResult

        # ---- 第一轮：缺 headcount，进入 pending ----
        session = SessionState(role="factory")
        mock_load.return_value = session

        mock_classify.return_value = IntentResult(
            intent="upload_job",
            structured_data={
                "city": "北京市",
                "job_category": "餐饮",
                "salary_floor_monthly": 7500,
                "pay_type": "月薪",
            },
            confidence=0.9,
        )

        replies1 = process(
            _msg("北京饭店招聘厨师，底薪7500+绩效，包吃不包住"), MagicMock(),
        )
        assert len(replies1) == 1
        # 应是追问招聘人数
        assert "招聘人数" in replies1[0].content
        # pending 已挂起
        assert session.pending_upload_intent == "upload_job"
        assert session.awaiting_field == "headcount"
        assert session.pending_upload["salary_floor_monthly"] == 7500

        # ---- 第二轮："2个人" 应走 pending guard ----
        # LLM 即使把它判成 follow_up 也不应触发 search_workers
        mock_classify.return_value = IntentResult(
            intent="follow_up",
            structured_data={},
            criteria_patch=[{"op": "update", "field": "headcount", "value": 2}],
            confidence=0.6,
        )
        mock_audit.audit_content_only.return_value = AuditResult(
            status="passed", reason="", matched_words=[],
        )
        mock_audit.write_audit_log_for_result = MagicMock()
        mock_ttl.return_value = 30
        fake_job = MagicMock()
        fake_job.id = 999
        mock_create_job.return_value = fake_job

        replies2 = process(_msg("2个人"), MagicMock())

        # 不应调用 search_workers
        mock_search_workers.assert_not_called()
        # 应入库
        mock_create_job.assert_called_once()
        # 回复包含“已入库”
        assert "已入库" in replies2[0].content

        # raw_text 包含两轮用户原文
        call_args = mock_create_job.call_args
        passed_raw_text = call_args[0][4]  # _create_job(data, user_ctx, audit, ttl, raw_text, ...)
        assert "北京饭店招聘厨师" in passed_raw_text
        assert "2个人" in passed_raw_text

        # pending 已清
        assert session.pending_upload == {}
        assert session.pending_upload_intent is None
        assert session.awaiting_field is None
        assert session.pending_raw_text_parts == []


class TestCancelDuringPending:
    """场景 3：pending 中 '取消' 清 pending。"""

    @patch("app.services.message_router.conversation_service.save_session")
    @patch("app.services.message_router.conversation_service.load_session")
    @patch("app.services.message_router.classify_intent")
    @patch("app.services.message_router.search_service.search_workers")
    @patch("app.services.message_router.search_service.search_jobs")
    @patch("app.services.upload_service._create_job")
    def test_cancel_clears_pending(
        self,
        mock_create_job,
        mock_search_jobs,
        mock_search_workers,
        mock_classify,
        mock_load,
        mock_save,
        stub_user_pipeline,
    ):
        # session 已处于 pending 状态
        future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        session = SessionState(
            role="factory",
            pending_upload={"city": "北京市", "job_category": "餐饮"},
            pending_upload_intent="upload_job",
            awaiting_field="headcount",
            pending_started_at=now,
            pending_updated_at=now,
            pending_expires_at=future,
            pending_raw_text_parts=["第一段文本"],
        )
        mock_load.return_value = session

        # LLM 可能将"取消"判成 chitchat 或 command；都不影响强规则
        mock_classify.return_value = IntentResult(
            intent="chitchat", structured_data={}, confidence=0.4,
        )

        replies = process(_msg("取消"), MagicMock())

        assert replies[0].content == PENDING_CANCELLED_REPLY
        assert session.pending_upload == {}
        assert session.pending_upload_intent is None
        # 不入库、不搜索
        mock_create_job.assert_not_called()
        mock_search_jobs.assert_not_called()
        mock_search_workers.assert_not_called()


class TestPendingTimeout:
    """场景：pending 超时后字段补丁应触发"草稿超时"提示。"""

    @patch("app.services.message_router.conversation_service.save_session")
    @patch("app.services.message_router.conversation_service.load_session")
    @patch("app.services.message_router.classify_intent")
    @patch("app.services.message_router.search_service.search_workers")
    @patch("app.services.upload_service._create_job")
    def test_expired_pending_with_patch_text_returns_expired_msg(
        self,
        mock_create_job,
        mock_search_workers,
        mock_classify,
        mock_load,
        mock_save,
        stub_user_pipeline,
    ):
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        long_ago = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
        session = SessionState(
            role="factory",
            pending_upload={"city": "北京市", "job_category": "餐饮"},
            pending_upload_intent="upload_job",
            awaiting_field="headcount",
            pending_started_at=long_ago,
            pending_updated_at=long_ago,
            pending_expires_at=past,
            pending_raw_text_parts=["第一段"],
        )
        mock_load.return_value = session
        mock_classify.return_value = IntentResult(
            intent="follow_up", confidence=0.5,
            criteria_patch=[{"op": "update", "field": "headcount", "value": 2}],
        )

        replies = process(_msg("2个人"), MagicMock())

        assert replies[0].content == PENDING_EXPIRED_REPLY
        # pending 被清空
        assert session.pending_upload == {}
        assert session.pending_upload_intent is None
        # 不入库、不搜索
        mock_create_job.assert_not_called()
        mock_search_workers.assert_not_called()


class TestHelpDoesNotClearPending:
    """场景 2：/帮助 不清 pending；用户回 '2个人' 仍能入库。"""

    @patch("app.services.message_router.conversation_service.save_session")
    @patch("app.services.message_router.conversation_service.load_session")
    @patch("app.services.message_router.classify_intent")
    @patch("app.services.message_router.command_service.execute")
    @patch("app.services.upload_service.audit_service")
    @patch("app.services.upload_service._read_ttl_days")
    @patch("app.services.upload_service._create_job")
    def test_help_command_preserves_pending(
        self,
        mock_create_job,
        mock_ttl,
        mock_audit,
        mock_cmd,
        mock_classify,
        mock_load,
        mock_save,
        stub_user_pipeline,
    ):
        from app.schemas.conversation import ReplyMessage
        from app.services.audit_service import AuditResult

        future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        session = SessionState(
            role="factory",
            pending_upload={
                "city": "北京市",
                "job_category": "餐饮",
                "salary_floor_monthly": 7500,
                "pay_type": "月薪",
            },
            pending_upload_intent="upload_job",
            awaiting_field="headcount",
            pending_started_at=now,
            pending_updated_at=now,
            pending_expires_at=future,
            pending_raw_text_parts=["北京饭店招聘厨师，底薪7500+绩效，包吃不包住"],
        )
        mock_load.return_value = session

        # 第一步：/帮助
        mock_classify.return_value = IntentResult(
            intent="command",
            structured_data={"command": "help", "args": ""},
            confidence=1.0,
        )
        mock_cmd.return_value = [ReplyMessage(userid="u1", content="help-reply")]

        replies1 = process(_msg("/帮助"), MagicMock())
        assert replies1[0].content == "help-reply"
        # pending 未清
        assert session.pending_upload_intent == "upload_job"
        assert session.awaiting_field == "headcount"
        assert "北京饭店招聘厨师，底薪7500+绩效，包吃不包住" in session.pending_raw_text_parts

        # 第二步：用户回 "2个人"，应能入库
        mock_classify.return_value = IntentResult(
            intent="follow_up",
            structured_data={},
            criteria_patch=[{"op": "update", "field": "headcount", "value": 2}],
            confidence=0.6,
        )
        mock_audit.audit_content_only.return_value = AuditResult(
            status="passed", reason="", matched_words=[],
        )
        mock_audit.write_audit_log_for_result = MagicMock()
        mock_ttl.return_value = 30
        fake_job = MagicMock()
        fake_job.id = 1234
        mock_create_job.return_value = fake_job

        replies2 = process(_msg("2个人"), MagicMock())
        assert "已入库" in replies2[0].content
        mock_create_job.assert_called_once()
        # raw_text 双段
        passed_raw_text = mock_create_job.call_args[0][4]
        assert "北京饭店招聘厨师" in passed_raw_text
        assert "2个人" in passed_raw_text


class TestPendingNoFieldFallback:
    """pending 中用户答非所问，不搜索，回兜底文案。"""

    @patch("app.services.message_router.conversation_service.save_session")
    @patch("app.services.message_router.conversation_service.load_session")
    @patch("app.services.message_router.classify_intent")
    @patch("app.services.message_router.search_service.search_workers")
    @patch("app.services.message_router.search_service.search_jobs")
    def test_irrelevant_text_returns_fallback_no_search(
        self,
        mock_search_jobs,
        mock_search_workers,
        mock_classify,
        mock_load,
        mock_save,
        stub_user_pipeline,
    ):
        future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        session = SessionState(
            role="factory",
            pending_upload={"city": "北京市", "job_category": "餐饮"},
            pending_upload_intent="upload_job",
            awaiting_field="headcount",
            pending_started_at=now,
            pending_updated_at=now,
            pending_expires_at=future,
            pending_raw_text_parts=["第一段"],
        )
        mock_load.return_value = session
        mock_classify.return_value = IntentResult(
            intent="chitchat", structured_data={}, confidence=0.3,
        )

        replies = process(_msg("还行吧"), MagicMock())

        assert "招聘人数" in replies[0].content
        mock_search_jobs.assert_not_called()
        mock_search_workers.assert_not_called()
        # pending 仍在
        assert session.pending_upload_intent == "upload_job"


# ---------------------------------------------------------------------------
# 辅助函数级测试
# ---------------------------------------------------------------------------

class TestCancelPredicate:
    @pytest.mark.parametrize("text", ["取消", "不发了", "算了", "先不发了", "不要了"])
    def test_full_match(self, text):
        intent = IntentResult(intent="chitchat", confidence=0.0)
        assert message_router._is_cancel(text, intent) is True

    @pytest.mark.parametrize("text", ["不发这个了", "先不发布岗位", "算了，换一个吧"])
    def test_prefix_match(self, text):
        intent = IntentResult(intent="chitchat", confidence=0.0)
        assert message_router._is_cancel(text, intent) is True

    @pytest.mark.parametrize("text", ["", "继续吧", "再来一次", "我不知道", "换一批"])
    def test_no_match(self, text):
        intent = IntentResult(intent="chitchat", confidence=0.0)
        assert message_router._is_cancel(text, intent) is False


class TestLooksLikePatch:
    @pytest.mark.parametrize("text", ["2", "30", "2个人", "招30人", "两个", "7500", "8千", "北京", "厨师"])
    def test_recognises_patch(self, text):
        assert message_router._looks_like_upload_patch(text) is True

    @pytest.mark.parametrize("text", ["", "你好啊", "随便聊聊"])
    def test_does_not_misclassify_chitchat(self, text):
        assert message_router._looks_like_upload_patch(text) is False


# ---------------------------------------------------------------------------
# Codex review 回归测试（4 处修复）
# ---------------------------------------------------------------------------

class TestParseHeadcount:
    """P2 修复：裸 4-5 位数字（薪资）不应被解析为 headcount。"""

    @pytest.mark.parametrize("text,expected", [
        # 带单位
        ("2个人", 2),
        ("招30人", 30),
        ("3位", 3),
        ("100名", 100),
        # 中文小数字
        ("两个", 2),
        ("十", 10),
        # 裸数字 1-3 位 ≤ 999
        ("2", 2),
        ("30", 30),
        ("999", 999),
    ])
    def test_valid_headcount(self, text, expected):
        assert message_router._parse_headcount_from_text(text) == expected

    @pytest.mark.parametrize("text", [
        "7500",        # 4 位，无单位 → 不该当人数（很可能是薪资）
        "10000",       # 5 位，无单位
        " 8888 ",      # 4 位，无单位
        "",
        "abc",
        "0",           # ≤ 0 不接受
    ])
    def test_rejects_naked_large_or_invalid(self, text):
        assert message_router._parse_headcount_from_text(text) is None

    def test_unit_present_allows_4_digits(self):
        # 带单位时允许 1-9999，例如"招1500人"是合理的（虽然罕见）
        assert message_router._parse_headcount_from_text("招1500人") == 1500


class TestExpiredHandlesNaiveDatetime:
    """P2 修复：pending_expires_at 是 naive ISO 字符串时不应抛 TypeError。"""

    def test_naive_past_treated_as_expired(self):
        naive_past = (datetime.now(timezone.utc) - timedelta(minutes=1)).replace(tzinfo=None).isoformat()
        s = SessionState(role="factory", pending_expires_at=naive_past)
        # 不应抛 TypeError；应返回 True（已过期）
        assert upload_service.is_pending_upload_expired(s) is True

    def test_naive_future_treated_as_active(self):
        naive_future = (datetime.now(timezone.utc) + timedelta(minutes=5)).replace(tzinfo=None).isoformat()
        s = SessionState(role="factory", pending_expires_at=naive_future)
        # 不应抛 TypeError；应返回 False（未过期）
        assert upload_service.is_pending_upload_expired(s) is False

    def test_garbage_string_treated_as_expired(self):
        s = SessionState(role="factory", pending_expires_at="not-a-datetime")
        assert upload_service.is_pending_upload_expired(s) is True


class TestPendingMaxRoundsFromFallback:
    """P1 修复：pending 中连续答非所问应在 follow_up_rounds 达到 MAX 时清空草稿。

    Stage B P2-1：mock intent 改为 follow_up（"答非所问但不是闲聊"），
    chitchat 按 spec §9.8 不再消耗追问计数，由 TestFixP2ChitchatDoesNotBurnRounds
    覆盖。这里保留"failed patch 触发 max rounds 退出"的原意。
    """

    @patch("app.services.message_router.conversation_service.save_session")
    @patch("app.services.message_router.conversation_service.load_session")
    @patch("app.services.message_router.classify_intent")
    @patch("app.services.message_router.search_service.search_workers")
    @patch("app.services.upload_service._create_job")
    def test_max_rounds_exit_clears_pending(
        self,
        mock_create_job,
        mock_search_workers,
        mock_classify,
        mock_load,
        mock_save,
        stub_user_pipeline,
    ):
        future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        # follow_up_rounds 已经 = MAX (=2)
        session = SessionState(
            role="factory",
            pending_upload={"city": "北京市", "job_category": "餐饮"},
            pending_upload_intent="upload_job",
            awaiting_field="headcount",
            pending_started_at=now,
            pending_updated_at=now,
            pending_expires_at=future,
            pending_raw_text_parts=["第一段"],
            follow_up_rounds=upload_service.MAX_FOLLOW_UP_ROUNDS,
        )
        mock_load.return_value = session
        mock_classify.return_value = IntentResult(
            intent="follow_up", structured_data={}, confidence=0.3,
        )

        replies = process(_msg("还行吧"), MagicMock())

        assert replies[0].content == PENDING_MAX_ROUNDS_REPLY
        # pending 已清
        assert session.pending_upload == {}
        assert session.pending_upload_intent is None
        # 不入库、不搜索
        mock_create_job.assert_not_called()
        mock_search_workers.assert_not_called()

    @patch("app.services.message_router.conversation_service.save_session")
    @patch("app.services.message_router.conversation_service.load_session")
    @patch("app.services.message_router.classify_intent")
    def test_fallback_increments_follow_up_rounds(
        self,
        mock_classify,
        mock_load,
        mock_save,
        stub_user_pipeline,
    ):
        future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        session = SessionState(
            role="factory",
            pending_upload={"city": "北京市", "job_category": "餐饮"},
            pending_upload_intent="upload_job",
            awaiting_field="headcount",
            pending_started_at=now,
            pending_updated_at=now,
            pending_expires_at=future,
            pending_raw_text_parts=["第一段"],
            follow_up_rounds=0,
        )
        mock_load.return_value = session
        mock_classify.return_value = IntentResult(
            intent="follow_up", structured_data={}, confidence=0.3,
        )

        process(_msg("还行吧"), MagicMock())

        # 兜底分支应递增 follow_up_rounds，避免无限循环
        assert session.follow_up_rounds == 1
        assert session.pending_upload_intent == "upload_job"  # 仍未清


class TestResetSearchPreservesPendingInvariants:
    """codex review §9.7：pending 存活时 /重新找 不应清 current_intent / follow_up_rounds，
    并应回复带"仍在发布"的提示文案。"""

    def test_reset_search_preserves_current_intent_when_pending_alive(self):
        from app.services import conversation_service

        session = SessionState(
            role="factory",
            current_intent="upload_job",
            search_criteria={"city": ["北京市"]},
            follow_up_rounds=1,
            pending_upload={"city": "北京市"},
            pending_upload_intent="upload_job",
            awaiting_field="headcount",
            pending_started_at=datetime.now(timezone.utc).isoformat(),
            pending_expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        )

        conversation_service.reset_search(session)

        # 搜索域已清
        assert session.search_criteria == {}
        assert session.candidate_snapshot is None
        assert session.shown_items == []
        # 上传域保留
        assert session.current_intent == "upload_job"
        assert session.follow_up_rounds == 1
        assert session.pending_upload_intent == "upload_job"

    def test_reset_search_clears_when_no_pending(self):
        """无 pending 时维持原行为：current_intent 和 follow_up_rounds 都清。"""
        from app.services import conversation_service

        session = SessionState(
            role="worker",
            current_intent="search_job",
            search_criteria={"city": ["北京市"]},
            follow_up_rounds=2,
        )

        conversation_service.reset_search(session)

        assert session.current_intent is None
        assert session.follow_up_rounds == 0
        assert session.search_criteria == {}

    def test_reset_search_command_returns_pending_hint(self):
        from app.services import command_service

        session = SessionState(
            role="factory",
            current_intent="upload_job",
            search_criteria={"city": ["北京市"]},
            pending_upload={"city": "北京市"},
            pending_upload_intent="upload_job",
            awaiting_field="headcount",
            pending_started_at=datetime.now(timezone.utc).isoformat(),
            pending_expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        )

        with patch("app.services.command_service.conversation_service.save_session"):
            replies = command_service.execute(
                "reset_search", "", _ctx("factory"), session, MagicMock(),
            )

        assert len(replies) == 1
        text = replies[0].content
        assert "搜索条件已重置" in text
        assert "招聘人数" in text  # awaiting_field=headcount → 显示名"招聘人数"
        assert "/取消" in text


class TestCurrentIntentPreservedDuringPending:
    """修复：pending 存活时，/帮助 等命令不应把 current_intent 覆盖成 'command'，
    否则后续图片消息走 _handle_image 时无法挂到上传记录上。"""

    @patch("app.services.message_router.conversation_service.save_session")
    @patch("app.services.message_router.conversation_service.load_session")
    @patch("app.services.message_router.classify_intent")
    @patch("app.services.message_router.command_service.execute")
    def test_help_during_pending_preserves_upload_intent(
        self,
        mock_cmd,
        mock_classify,
        mock_load,
        mock_save,
        stub_user_pipeline,
    ):
        from app.schemas.conversation import ReplyMessage

        future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        session = SessionState(
            role="factory",
            current_intent="upload_job",
            pending_upload={"city": "北京市"},
            pending_upload_intent="upload_job",
            awaiting_field="headcount",
            pending_started_at=now,
            pending_updated_at=now,
            pending_expires_at=future,
            pending_raw_text_parts=["第一段"],
        )
        mock_load.return_value = session
        mock_classify.return_value = IntentResult(
            intent="command",
            structured_data={"command": "help", "args": ""},
            confidence=1.0,
        )
        mock_cmd.return_value = [ReplyMessage(userid="u1", content="help-reply")]

        process(_msg("/帮助"), MagicMock())

        # current_intent 应保持为 pending 的上传 intent，让后续图片仍能挂到 Job
        assert session.current_intent == "upload_job"

    @patch("app.services.message_router.conversation_service.save_session")
    @patch("app.services.message_router.conversation_service.load_session")
    @patch("app.services.message_router.classify_intent")
    @patch("app.services.message_router.command_service.execute")
    def test_command_without_pending_overwrites_current_intent(
        self,
        mock_cmd,
        mock_classify,
        mock_load,
        mock_save,
        stub_user_pipeline,
    ):
        from app.schemas.conversation import ReplyMessage

        # 没有 pending：/帮助 后 current_intent 应被覆盖为 "command"（保持原行为）
        session = SessionState(role="factory", current_intent="search_worker")
        mock_load.return_value = session
        mock_classify.return_value = IntentResult(
            intent="command",
            structured_data={"command": "help", "args": ""},
            confidence=1.0,
        )
        mock_cmd.return_value = [ReplyMessage(userid="u1", content="help-reply")]

        process(_msg("/帮助"), MagicMock())

        assert session.current_intent == "command"
