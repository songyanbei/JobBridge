"""Stage B 多轮上传 / 搜索质量单测。

覆盖 docs/multi-turn-upload-stage-b-implementation.md §4 必测用例：
- intent_service 规整层：job_category 同义词归并、非法薪资丢弃、list 去空去重
- message_router._run_search 默认 criteria 合并：worker 简历 expected_* 兜底
- search_service 0/低召回 fallback 显式分步 + 日志
- 0 命中文案不伪装成推荐
- Stage A 链路不回退（headcount-only criteria 仍返回空）
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.llm.base import IntentResult
from app.schemas.conversation import SessionState
from app.services import (
    command_service,
    conversation_service,
    intent_service,
    message_router,
    search_service,
    upload_service,
)
from app.services.command_service import (
    CANCEL_PENDING_NO_DRAFT,
    CANCEL_PENDING_OK,
    RESET_SEARCH_EMPTY,
    RESET_SEARCH_PENDING_FMT,
    RESET_SEARCH_SUCCESS,
    SWITCH_DIRECTION_PENDING_FMT,
    SWITCH_JOB_OK,
)
from app.services.intent_service import _match_command
from app.services.search_service import (
    NO_JOB_MATCH_REPLY,
    NO_WORKER_MATCH_REPLY,
    _broaden_job_categories,
    _run_job_fallback_steps,
    _run_resume_fallback_steps,
    _strip_optional_filters,
)
from app.services.user_service import UserContext
from app.wecom.callback import WeComMessage


def _ctx(role="worker", external_userid="u1"):
    return UserContext(
        external_userid=external_userid, role=role, status="active",
        display_name="张三", company=None, contact_person=None, phone=None,
        can_search_jobs=role in ("worker", "broker"),
        can_search_workers=role in ("factory", "broker"),
        is_first_touch=False, should_welcome=False,
    )


# ---------------------------------------------------------------------------
# §3.2 字段规整层
# ---------------------------------------------------------------------------

class TestJobCategoryNormalization:
    def test_canonical_passthrough(self):
        assert intent_service._normalize_job_category_value("餐饮") == "餐饮"
        assert intent_service._normalize_job_category_value("电子厂") == "电子厂"

    def test_synonym_mapping_chef_to_catering(self):
        assert intent_service._normalize_job_category_value("厨师") == "餐饮"
        assert intent_service._normalize_job_category_value("后厨") == "餐饮"
        assert intent_service._normalize_job_category_value("饭店") == "餐饮"

    def test_synonym_mapping_packing_to_logistics(self):
        assert intent_service._normalize_job_category_value("打包") == "物流仓储"
        assert intent_service._normalize_job_category_value("分拣") == "物流仓储"
        assert intent_service._normalize_job_category_value("仓库") == "物流仓储"

    def test_synonym_mapping_assembly_to_electronics(self):
        assert intent_service._normalize_job_category_value("组装") == "电子厂"
        assert intent_service._normalize_job_category_value("SMT".lower()) == "电子厂"

    def test_unknown_value_kept(self):
        assert intent_service._normalize_job_category_value("月嫂") == "月嫂"

    def test_empty_value_returns_none(self):
        assert intent_service._normalize_job_category_value("") is None
        assert intent_service._normalize_job_category_value("   ") is None
        assert intent_service._normalize_job_category_value(None) is None


class TestNormalizeStringList:
    def test_str_to_list(self):
        assert intent_service._normalize_string_list("北京市") == ["北京市"]

    def test_list_dedup_and_strip(self):
        assert intent_service._normalize_string_list(
            ["苏州市", " 苏州市 ", "", None, "昆山市"],
        ) == ["苏州市", "昆山市"]

    def test_none_returns_empty(self):
        assert intent_service._normalize_string_list(None) == []


class TestNormalizeStructuredData:
    def test_search_intent_city_to_list(self):
        out = intent_service._normalize_structured_data(
            {"city": "苏州市", "job_category": "厨师"},
            role="worker", intent="search_job",
        )
        assert out["city"] == ["苏州市"]
        assert out["job_category"] == ["餐饮"]

    def test_upload_intent_city_to_scalar(self):
        out = intent_service._normalize_structured_data(
            {"city": ["北京市"], "job_category": ["厨师"], "salary_floor_monthly": "7500", "headcount": 2},
            role="factory", intent="upload_job",
        )
        assert out["city"] == "北京市"
        assert out["job_category"] == "餐饮"
        assert out["salary_floor_monthly"] == 7500
        assert out["headcount"] == 2

    def test_invalid_salary_dropped(self):
        out = intent_service._normalize_structured_data(
            {"city": "北京市", "salary_floor_monthly": "很多", "headcount": 0},
            role="factory", intent="upload_job",
        )
        assert "salary_floor_monthly" not in out
        # headcount 0 不在 [1, 9999]，丢弃
        assert "headcount" not in out

    def test_negative_headcount_dropped(self):
        out = intent_service._normalize_structured_data(
            {"city": "北京市", "headcount": -1},
            role="factory", intent="upload_job",
        )
        assert "headcount" not in out

    def test_salary_ceiling_below_floor_dropped(self):
        out = intent_service._normalize_structured_data(
            {"city": "北京市", "salary_floor_monthly": 8000, "salary_ceiling_monthly": 5000},
            role="factory", intent="upload_job",
        )
        assert out["salary_floor_monthly"] == 8000
        assert "salary_ceiling_monthly" not in out

    def test_expected_lists_preserved_with_mapping(self):
        out = intent_service._normalize_structured_data(
            {"expected_cities": ["无锡", "无锡"], "expected_job_categories": ["厨师", "保洁"]},
            role="worker", intent="upload_resume",
        )
        assert out["expected_cities"] == ["无锡"]
        assert out["expected_job_categories"] == ["餐饮", "保洁"]

    def test_unknown_keys_already_filtered_by_sanitize(self):
        # 规整层不丢未知 key（_sanitize_intent_result 已经做了），
        # 这里只是确保 normalize 不抛异常
        out = intent_service._normalize_structured_data(
            {"city": "北京市", "weird_key": "x"},
            role="factory", intent="upload_job",
        )
        # weird_key 透传（normalize 不再过滤未知 key — 那是 sanitize 的职责）
        assert out["city"] == "北京市"


class TestNormalizeCriteriaPatch:
    def test_list_field_patch_normalized(self):
        out = intent_service._normalize_criteria_patch([
            {"op": "update", "field": "city", "value": "苏州市"},
            {"op": "add", "field": "job_category", "value": ["厨师"]},
        ])
        assert out[0]["value"] == ["苏州市"]
        assert out[1]["value"] == ["餐饮"]

    def test_invalid_int_patch_dropped(self):
        out = intent_service._normalize_criteria_patch([
            {"op": "update", "field": "salary_floor_monthly", "value": "很多"},
        ])
        assert out == []

    def test_remove_with_null_value_kept(self):
        out = intent_service._normalize_criteria_patch([
            {"op": "remove", "field": "city", "value": None},
        ])
        assert out == [{"op": "remove", "field": "city", "value": None}]

    def test_unknown_field_dropped(self):
        out = intent_service._normalize_criteria_patch([
            {"op": "update", "field": "weird", "value": "x"},
        ])
        assert out == []


# ---------------------------------------------------------------------------
# §3.3 默认 criteria 合并
# ---------------------------------------------------------------------------

class TestApplyDefaultCriteria:
    def test_session_criteria_fills_missing(self):
        session = SessionState(role="worker", search_criteria={"city": ["无锡"]})
        ctx = _ctx("worker")
        composed = message_router._apply_default_criteria(
            {"job_category": ["电子厂"]}, session, ctx, MagicMock(), "search_job",
        )
        assert composed["city"] == ["无锡"]
        assert composed["job_category"] == ["电子厂"]

    def test_existing_value_not_overwritten(self):
        session = SessionState(role="worker", search_criteria={"city": ["无锡"]})
        ctx = _ctx("worker")
        composed = message_router._apply_default_criteria(
            {"city": ["昆山"]}, session, ctx, MagicMock(), "search_job",
        )
        assert composed["city"] == ["昆山"]

    def test_empty_value_treated_as_missing(self):
        session = SessionState(role="worker", search_criteria={"city": ["无锡"]})
        ctx = _ctx("worker")
        composed = message_router._apply_default_criteria(
            {"city": []}, session, ctx, MagicMock(), "search_job",
        )
        # 空 list 视为未提供，可被 session 默认覆盖
        assert composed["city"] == ["无锡"]

    @patch("app.services.message_router._load_worker_resume_defaults")
    def test_worker_resume_defaults_used(self, mock_load):
        mock_load.return_value = {"city": ["无锡"], "job_category": ["电子厂"]}
        session = SessionState(role="worker")
        ctx = _ctx("worker")
        composed = message_router._apply_default_criteria(
            {}, session, ctx, MagicMock(), "search_job",
        )
        assert composed["city"] == ["无锡"]
        assert composed["job_category"] == ["电子厂"]

    @patch("app.services.message_router._load_worker_resume_defaults")
    def test_factory_does_not_use_resume_defaults(self, mock_load):
        mock_load.return_value = {"city": ["无锡"]}
        session = SessionState(role="factory")
        ctx = _ctx("factory")
        composed = message_router._apply_default_criteria(
            {}, session, ctx, MagicMock(), "search_worker",
        )
        # factory + search_worker 不查 worker 简历
        mock_load.assert_not_called()
        assert "city" not in composed


class TestBuildUploadAndSearchCriteria:
    def test_factory_upload_job_to_search_workers(self):
        out = message_router._build_upload_and_search_criteria(
            {"city": "北京市", "job_category": "餐饮", "salary_ceiling_monthly": 8000},
            direction="search_worker",
        )
        assert out["city"] == ["北京市"]
        assert out["job_category"] == ["餐饮"]
        assert out["salary_ceiling_monthly"] == 8000

    def test_worker_upload_resume_to_search_jobs(self):
        out = message_router._build_upload_and_search_criteria(
            {
                "expected_cities": ["无锡"],
                "expected_job_categories": ["电子厂"],
                "salary_expect_floor_monthly": 6000,
            },
            direction="search_job",
        )
        assert out["city"] == ["无锡"]
        assert out["job_category"] == ["电子厂"]
        assert out["salary_floor_monthly"] == 6000

    def test_empty_input(self):
        assert message_router._build_upload_and_search_criteria({}, "search_worker") == {}


# ---------------------------------------------------------------------------
# §3.4 0 命中 fallback 显式分步
# ---------------------------------------------------------------------------

class TestJobFallbackSteps:
    @patch("app.services.search_service._query_jobs")
    def test_relax_salary_step_when_initial_zero(self, mock_query):
        # 第一次调用 (relax_salary) 命中 3 条；第二次 (drop_optional) 不会被需要
        mock_query.return_value = [MagicMock()] * 3
        criteria = {"city": ["北京"], "job_category": ["餐饮"], "salary_floor_monthly": 8000}
        out = _run_job_fallback_steps(criteria, [], top_n=3, limit=50, db=MagicMock())
        assert len(out) == 3
        # 第一次的 criteria 应当是放宽过的
        called_criteria = mock_query.call_args_list[0][0][0]
        assert called_criteria["salary_floor_monthly"] == 7200  # 8000 * 0.9

    @patch("app.services.search_service._query_jobs")
    def test_no_better_result_keeps_initial(self, mock_query):
        # fallback 始终没召回；保留 initial（空）
        mock_query.return_value = []
        criteria = {"city": ["北京"], "job_category": ["餐饮"], "salary_floor_monthly": 8000}
        out = _run_job_fallback_steps(criteria, [], top_n=3, limit=50, db=MagicMock())
        assert out == []

    @patch("app.services.search_service._query_jobs")
    def test_drop_optional_filters_step(self, mock_query):
        # 模拟两次调用：放宽薪资 0 命中；drop optional 命中 2
        mock_query.side_effect = [[], [MagicMock(), MagicMock()]]
        criteria = {
            "city": ["北京"], "job_category": ["餐饮"],
            "salary_floor_monthly": 8000, "gender_required": "男",
        }
        out = _run_job_fallback_steps(criteria, [], top_n=3, limit=50, db=MagicMock())
        assert len(out) == 2
        # 第二次 drop_optional 应该不再带 gender_required
        second_criteria = mock_query.call_args_list[1][0][0]
        assert "gender_required" not in second_criteria


class TestResumeFallbackSteps:
    @patch("app.services.search_service._query_resumes")
    def test_relax_ceiling(self, mock_query):
        mock_query.return_value = [MagicMock()] * 3
        criteria = {"city": ["北京"], "job_category": ["餐饮"], "salary_ceiling_monthly": 5000}
        out = _run_resume_fallback_steps(criteria, [], top_n=3, limit=50, db=MagicMock())
        assert len(out) == 3
        called = mock_query.call_args_list[0][0][0]
        assert called["salary_ceiling_monthly"] == 5500  # ceil(5000 * 1.1)


class TestStripOptionalFilters:
    def test_keep_city_and_job_category(self):
        original = {
            "city": ["北京"], "job_category": ["餐饮"],
            "gender_required": "男", "is_long_term": True,
        }
        out = _strip_optional_filters(original, ("gender_required", "is_long_term"))
        assert out["city"] == ["北京"]
        assert out["job_category"] == ["餐饮"]
        assert "gender_required" not in out
        assert "is_long_term" not in out


# ---------------------------------------------------------------------------
# §3.5 0 命中文案
# ---------------------------------------------------------------------------

class TestNoMatchReply:
    def test_no_job_reply_does_not_pretend_recommendation(self):
        assert "暂未找到" in NO_JOB_MATCH_REPLY
        assert "推荐" not in NO_JOB_MATCH_REPLY
        assert "为您找到" not in NO_JOB_MATCH_REPLY

    def test_no_worker_reply_does_not_pretend_recommendation(self):
        assert "暂未找到" in NO_WORKER_MATCH_REPLY
        assert "推荐" not in NO_WORKER_MATCH_REPLY
        assert "为您找到" not in NO_WORKER_MATCH_REPLY


# ---------------------------------------------------------------------------
# Stage A 不回退：headcount-only 仍返回空
# ---------------------------------------------------------------------------

class TestStageAGuardStillHolds:
    def test_query_jobs_still_blocks_headcount_only(self):
        from app.services.search_service import _query_jobs
        db = MagicMock()
        assert _query_jobs({"headcount": 2}, 50, db) == []
        db.query.assert_not_called()


# ---------------------------------------------------------------------------
# 修复点回归（针对 codex 第二轮审查）
# ---------------------------------------------------------------------------

def _msg(content: str, userid: str = "u1") -> WeComMessage:
    return WeComMessage(
        msg_id="m1", from_user=userid, to_user="bot",
        msg_type="text", content=content, media_id="",
        image_url="", create_time=1700000000,
    )


class TestFixP1ResumeDefaultsReachSearch:
    """P1-1 回归：worker 空 criteria 也必须走到 _apply_default_criteria。"""

    @patch("app.services.message_router.search_service.search_jobs")
    @patch("app.services.message_router._load_worker_resume_defaults")
    def test_handle_search_no_short_circuit_on_empty_criteria(
        self, mock_load, mock_search,
    ):
        # 模拟 worker 已有简历兜底
        mock_load.return_value = {"city": ["无锡"], "job_category": ["电子厂"]}
        mock_search.return_value = search_service.SearchResult(
            reply_text="为您找到 2 个匹配岗位", result_count=2,
        )
        ctx = _ctx("worker")
        session = SessionState(role="worker")
        intent = IntentResult(intent="search_job", structured_data={}, confidence=0.7)
        replies = message_router._handle_search(
            intent, _msg("看看新岗位"), ctx, session, MagicMock(),
        )
        # 关键断言：search_jobs 被调用，且收到的 criteria 是简历默认条件
        mock_search.assert_called_once()
        called_criteria = mock_search.call_args[0][0]
        assert called_criteria.get("city") == ["无锡"]
        assert called_criteria.get("job_category") == ["电子厂"]
        assert "为您找到" in replies[0].content

    @patch("app.services.message_router.search_service.search_jobs")
    @patch("app.services.message_router._load_worker_resume_defaults")
    def test_handle_follow_up_no_short_circuit_on_empty_criteria(
        self, mock_load, mock_search,
    ):
        mock_load.return_value = {"city": ["无锡"], "job_category": ["电子厂"]}
        mock_search.return_value = search_service.SearchResult(
            reply_text="为您找到 1 个匹配岗位", result_count=1,
        )
        ctx = _ctx("worker")
        session = SessionState(role="worker")
        intent = IntentResult(intent="follow_up", criteria_patch=[], confidence=0.6)
        replies = message_router._handle_follow_up(
            intent, _msg("再看看"), ctx, session, MagicMock(),
        )
        mock_search.assert_called_once()
        called_criteria = mock_search.call_args[0][0]
        assert called_criteria.get("city") == ["无锡"]


class TestFixP2ChitchatDoesNotBurnRounds:
    """P2-1 回归：upload_collecting 中 chitchat 不递增 follow_up_rounds。"""

    @patch("app.services.message_router.upload_service.is_pending_upload_expired",
           return_value=False)
    def test_chitchat_keeps_pending_and_counter(self, _):
        ctx = _ctx("factory")
        session = SessionState(
            role="factory",
            pending_upload={"city": "北京市", "job_category": "餐饮", "salary_floor_monthly": 7500, "pay_type": "月薪"},
            pending_upload_intent="upload_job",
            awaiting_field="headcount",
            pending_started_at=datetime.now(timezone.utc).isoformat(),
            pending_expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
            pending_raw_text_parts=["北京饭店招聘厨师"],
            follow_up_rounds=0,
        )
        intent = IntentResult(intent="chitchat", structured_data={}, confidence=0.0)
        replies = message_router._handle_pending_upload(
            "你好", intent, _msg("你好"), ctx, session, MagicMock(),
        )
        assert replies is not None
        # follow_up_rounds 不动
        assert session.follow_up_rounds == 0
        # pending 仍在
        assert session.pending_upload_intent == "upload_job"
        assert session.awaiting_field == "headcount"
        # 文案带 "招聘人数" 提醒
        assert "招聘人数" in replies[0].content


class TestFixP2ResetSearchPending:
    """P2-2 回归：/重新找 在 pending 时无条件给 pending 提醒，不论是否有搜索状态。"""

    @patch("app.services.command_service.conversation_service.save_session")
    def test_reset_search_with_pending_only_no_search_state(self, _save):
        ctx = UserContext(
            external_userid="u1", role="factory", status="active",
            display_name="X", company=None, contact_person=None, phone=None,
            can_search_jobs=False, can_search_workers=True,
            is_first_touch=False, should_welcome=False,
        )
        session = SessionState(
            role="factory",
            pending_upload={"city": "北京市"},
            pending_upload_intent="upload_job",
            awaiting_field="headcount",
        )
        replies = command_service.execute(
            "reset_search", "", ctx, session, MagicMock(),
        )
        assert replies[0].content != RESET_SEARCH_EMPTY
        assert "仍在发布" in replies[0].content
        assert "招聘人数" in replies[0].content

    @patch("app.services.command_service.conversation_service.save_session")
    @patch("app.services.command_service.conversation_service.reset_search")
    def test_reset_search_with_pending_and_search_state(self, mock_reset, _save):
        ctx = UserContext(
            external_userid="u1", role="factory", status="active",
            display_name="X", company=None, contact_person=None, phone=None,
            can_search_jobs=False, can_search_workers=True,
            is_first_touch=False, should_welcome=False,
        )
        session = SessionState(
            role="factory",
            search_criteria={"city": ["北京"]},
            pending_upload={"city": "北京市"},
            pending_upload_intent="upload_job",
            awaiting_field="headcount",
        )
        replies = command_service.execute(
            "reset_search", "", ctx, session, MagicMock(),
        )
        # 搜索状态被重置
        mock_reset.assert_called_once()
        # 但仍给 pending 提醒
        assert "仍在发布" in replies[0].content


class TestFixP2BrokerDirectionPending:
    """P2-3 回归：broker 切方向不重置 pending follow_up_rounds + 加 pending 提醒。"""

    @patch("app.services.command_service.conversation_service.save_session")
    def test_broker_switch_keeps_pending_counter(self, _save):
        ctx = UserContext(
            external_userid="u1", role="broker", status="active",
            display_name="X", company=None, contact_person=None, phone=None,
            can_search_jobs=True, can_search_workers=True,
            is_first_touch=False, should_welcome=False,
        )
        session = SessionState(
            role="broker",
            search_criteria={"city": ["北京"]},
            pending_upload={"city": "北京市"},
            pending_upload_intent="upload_job",
            awaiting_field="headcount",
            follow_up_rounds=1,  # pending 已经追问过 1 次
        )
        replies = command_service.execute(
            "switch_to_job", "", ctx, session, MagicMock(),
        )
        # follow_up_rounds 不被清零（因为 pending 在）
        assert session.follow_up_rounds == 1
        # 检索状态仍然清空
        assert session.search_criteria == {}
        assert session.broker_direction == "search_job"
        # 回复包含 pending 提醒
        assert replies[0].content != SWITCH_JOB_OK
        assert "仍在发布" in replies[0].content
        assert "招聘人数" in replies[0].content

    @patch("app.services.command_service.conversation_service.save_session")
    def test_broker_switch_without_pending_resets_rounds(self, _save):
        ctx = UserContext(
            external_userid="u1", role="broker", status="active",
            display_name="X", company=None, contact_person=None, phone=None,
            can_search_jobs=True, can_search_workers=True,
            is_first_touch=False, should_welcome=False,
        )
        session = SessionState(role="broker", follow_up_rounds=2)
        replies = command_service.execute(
            "switch_to_job", "", ctx, session, MagicMock(),
        )
        # 没 pending，照旧清零
        assert session.follow_up_rounds == 0
        assert replies[0].content == SWITCH_JOB_OK


class TestFixP1CancelCommandClearsPending:
    """P1-2 回归：/取消 注册为 command + cancel_pending handler 清 pending。"""

    def test_match_command_routes_slash_cancel(self):
        # 闭合 LLM 误判旁路：deterministic _match_command 必须先于 LLM
        assert _match_command("/取消") == ("cancel_pending", "")

    @patch("app.services.command_service.conversation_service.save_session")
    def test_cancel_pending_handler_clears_pending(self, _save):
        ctx = UserContext(
            external_userid="u1", role="factory", status="active",
            display_name="X", company=None, contact_person=None, phone=None,
            can_search_jobs=False, can_search_workers=True,
            is_first_touch=False, should_welcome=False,
        )
        session = SessionState(
            role="factory",
            pending_upload={"city": "北京市"},
            pending_upload_intent="upload_job",
            awaiting_field="headcount",
            pending_started_at=datetime.now(timezone.utc).isoformat(),
            pending_expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
            pending_raw_text_parts=["北京饭店招聘厨师"],
            follow_up_rounds=1,
        )
        replies = command_service.execute(
            "cancel_pending", "", ctx, session, MagicMock(),
        )
        assert replies[0].content == CANCEL_PENDING_OK
        assert session.pending_upload == {}
        assert session.pending_upload_intent is None
        assert session.awaiting_field is None
        assert session.pending_raw_text_parts == []
        assert session.follow_up_rounds == 0

    def test_cancel_pending_no_draft(self):
        ctx = UserContext(
            external_userid="u1", role="factory", status="active",
            display_name="X", company=None, contact_person=None, phone=None,
            can_search_jobs=False, can_search_workers=True,
            is_first_touch=False, should_welcome=False,
        )
        session = SessionState(role="factory")
        replies = command_service.execute(
            "cancel_pending", "", ctx, session, MagicMock(),
        )
        assert replies[0].content == CANCEL_PENDING_NO_DRAFT

    @patch("app.services.command_service.conversation_service.save_session")
    def test_cancel_alias_also_clears_pending(self, _save):
        """codex P2：兜底兼容 LLM 直出 command='cancel' 不带 _pending 后缀。"""
        ctx = UserContext(
            external_userid="u1", role="factory", status="active",
            display_name="X", company=None, contact_person=None, phone=None,
            can_search_jobs=False, can_search_workers=True,
            is_first_touch=False, should_welcome=False,
        )
        session = SessionState(
            role="factory",
            pending_upload={"city": "北京市"},
            pending_upload_intent="upload_job",
            awaiting_field="headcount",
        )
        replies = command_service.execute(
            "cancel", "", ctx, session, MagicMock(),
        )
        assert replies[0].content == CANCEL_PENDING_OK
        assert session.pending_upload_intent is None


class TestFixP3JobCategoryBroadening:
    """P3 回归：fallback 把工种细分类映射到 canonical 大类。"""

    def test_broaden_maps_chef_to_catering(self):
        out = _broaden_job_categories({"city": ["北京"], "job_category": ["厨师"]})
        assert out == {"city": ["北京"], "job_category": ["餐饮"]}

    def test_broaden_str_value(self):
        out = _broaden_job_categories({"city": ["北京"], "job_category": "打包"})
        assert out["job_category"] == ["物流仓储"]

    def test_broaden_dedup_canonical(self):
        out = _broaden_job_categories(
            {"city": ["北京"], "job_category": ["厨师", "服务员", "餐饮"]},
        )
        # 全部映射到 餐饮，去重后只剩一个
        assert out["job_category"] == ["餐饮"]

    def test_broaden_no_change_when_already_canonical(self):
        out = _broaden_job_categories(
            {"city": ["北京"], "job_category": ["餐饮"]},
        )
        assert out is None

    def test_broaden_no_change_when_no_synonym(self):
        out = _broaden_job_categories(
            {"city": ["北京"], "job_category": ["月嫂"]},
        )
        # 月嫂 不在同义词字典里，保留原值，不算变化
        assert out is None

    def test_broaden_none_when_empty(self):
        assert _broaden_job_categories({"city": ["北京"]}) is None
        assert _broaden_job_categories({"job_category": []}) is None

    @patch("app.services.search_service._query_jobs")
    def test_fallback_step_uses_broadening_to_recover(self, mock_query):
        # 模拟 codex 复现：DB 仅在 job_category=餐饮 时命中。
        # initial 用 厨师 查 → 0；fallback Step "broaden_job_category" 命中
        def fake_query(criteria, *args, **kwargs):
            cats = criteria.get("job_category") or []
            if isinstance(cats, str):
                cats = [cats]
            if "餐饮" in cats:
                return [MagicMock(), MagicMock()]
            return []
        mock_query.side_effect = fake_query
        criteria = {"city": ["北京"], "job_category": ["厨师"]}
        out = _run_job_fallback_steps(criteria, [], top_n=3, limit=50, db=MagicMock())
        assert len(out) == 2

    @patch("app.services.search_service._query_resumes")
    def test_resume_fallback_step_uses_broadening(self, mock_query):
        def fake_query(criteria, *args, **kwargs):
            cats = criteria.get("job_category") or []
            if isinstance(cats, str):
                cats = [cats]
            return [MagicMock()] if "餐饮" in cats else []
        mock_query.side_effect = fake_query
        criteria = {"city": ["北京"], "job_category": ["厨师"]}
        out = _run_resume_fallback_steps(criteria, [], top_n=3, limit=50, db=MagicMock())
        assert len(out) == 1


class TestSetBrokerDirectionPreservesPending:
    """conversation_service.set_broker_direction：pending 在时不动 follow_up_rounds。"""

    def test_pending_keeps_rounds(self):
        session = SessionState(
            role="broker",
            pending_upload_intent="upload_job",
            awaiting_field="headcount",
            follow_up_rounds=1,
        )
        err = conversation_service.set_broker_direction(session, "search_job")
        assert err is None
        assert session.follow_up_rounds == 1

    def test_no_pending_resets_rounds(self):
        session = SessionState(role="broker", follow_up_rounds=2)
        err = conversation_service.set_broker_direction(session, "search_worker")
        assert err is None
        assert session.follow_up_rounds == 0
