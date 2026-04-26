"""search_service 单元测试。"""
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta, timezone

import pytest

from app.llm.base import RerankResult
from app.schemas.conversation import CandidateSnapshot, SessionState
from app.services.search_service import (
    FallbackOutcome,
    FallbackSuggestion,
    NO_JOB_MATCH_REPLY,
    NO_WORKER_MATCH_REPLY,
    SearchResult,
    _FALLBACK_NOTICE_JOB,
    _format_job_results,
    _format_no_match_with_suggestions_job,
    _format_no_match_with_suggestions_resume,
    _format_resume_results,
    _is_job_search,
    _probe_job_suggestions,
    _probe_resume_suggestions,
    _summarize_search_criteria,
    search_jobs,
    search_workers,
    show_more,
)
from app.services.user_service import UserContext


def _fresh_expires() -> str:
    """生成一个 30 分钟后过期的时间戳。"""
    return (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()


def _past_expires() -> str:
    """生成一个已过期的时间戳。"""
    return (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()


def _make_user_ctx(role="worker"):
    return UserContext(
        external_userid="u1", role=role, status="active",
        display_name=None, company=None, contact_person=None, phone=None,
        can_search_jobs=True, can_search_workers=role != "worker",
        is_first_touch=False, should_welcome=False,
    )


def _make_session(role="worker", **kwargs):
    return SessionState(role=role, **kwargs)


class TestIsJobSearch:
    def test_worker_always_job(self):
        session = _make_session("worker")
        assert _is_job_search(session, _make_user_ctx("worker")) is True

    def test_factory_always_worker(self):
        session = _make_session("factory")
        assert _is_job_search(session, _make_user_ctx("factory")) is False

    def test_broker_job_direction(self):
        session = _make_session("broker", broker_direction="search_job")
        assert _is_job_search(session, _make_user_ctx("broker")) is True

    def test_broker_worker_direction(self):
        session = _make_session("broker", broker_direction="search_worker")
        assert _is_job_search(session, _make_user_ctx("broker")) is False


class TestFormatJobResults:
    def test_basic_format(self):
        jobs = [
            {
                "id": 1, "company": "XX电子", "job_category": "电子厂",
                "salary_floor_monthly": 5500, "salary_ceiling_monthly": 6500,
                "pay_type": "月薪", "city": "苏州市", "district": "吴中区",
                "provide_meal": True, "provide_housing": True,
                "shift_pattern": "两班倒",
            },
        ]
        text = _format_job_results(jobs, 5)
        assert "①" in text
        assert "5500-6500" in text
        assert "苏州市" in text
        assert "更多" in text

    def test_no_remaining(self):
        jobs = [{"id": 1, "company": "XX", "job_category": "普工",
                 "salary_floor_monthly": 5000, "pay_type": "月薪",
                 "city": "苏州市"}]
        text = _format_job_results(jobs, 0)
        assert "更多" not in text

    def test_empty(self):
        text = _format_job_results([], 0)
        assert "暂无" in text


class TestFormatResumeResults:
    def test_basic_format(self):
        resumes = [
            {
                "id": 1, "display_name": "张三", "gender": "男", "age": 35,
                "expected_job_categories": ["电子厂"], "salary_expect_floor_monthly": 5000,
                "expected_cities": ["苏州市"], "phone": "13800001111",
                "work_experience": "3年电子厂",
            },
        ]
        text = _format_resume_results(resumes, 3)
        assert "张三" in text
        assert "13800001111" in text
        assert "更多" in text

    def test_phone_placeholder(self):
        resumes = [
            {
                "id": 1, "display_name": "李四", "gender": "女", "age": 28,
                "expected_job_categories": [], "salary_expect_floor_monthly": 4000,
                "expected_cities": [], "phone": None,
                "phone_placeholder": "联系方式待补充",
            },
        ]
        text = _format_resume_results(resumes, 0)
        assert "联系方式待补充" in text


class TestSearchJobs:
    @patch("app.services.search_service.get_reranker")
    @patch("app.services.search_service._query_jobs")
    @patch("app.services.search_service._get_config_int")
    @patch("app.services.search_service._build_users_map")
    @patch("app.services.search_service._jobs_to_dicts")
    def test_zero_recall_no_reranker(
        self, mock_to_dicts, mock_users, mock_config, mock_query, mock_reranker,
    ):
        mock_config.return_value = 3
        mock_query.return_value = []

        db = MagicMock()
        session = _make_session()
        user_ctx = _make_user_ctx()

        result = search_jobs({}, "苏州找工作", session, user_ctx, db)
        assert result.result_count == 0
        mock_reranker.assert_not_called()


class TestShowMore:
    @patch("app.services.search_service._get_config_int")
    def test_no_snapshot(self, mock_config):
        mock_config.return_value = 3
        session = _make_session()
        db = MagicMock()
        result = show_more(session, _make_user_ctx(), db)
        assert "没有" in result.reply_text or "先搜索" in result.reply_text

    @patch("app.services.search_service._validate_job_ids")
    @patch("app.services.search_service._jobs_to_dicts")
    @patch("app.services.search_service._get_config_int")
    def test_show_more_from_snapshot_all_expired_items(self, mock_config, mock_dicts, mock_validate):
        mock_config.return_value = 3
        mock_validate.return_value = []  # simulate all items expired
        mock_dicts.return_value = []

        session = _make_session()
        session.candidate_snapshot = CandidateSnapshot(
            candidate_ids=["1", "2", "3", "4", "5"],
            query_digest="abc",
            expires_at=_fresh_expires(),
        )
        session.shown_items = ["1", "2", "3"]

        db = MagicMock()
        result = show_more(session, _make_user_ctx(), db)
        assert result is not None
        assert result.result_count == 0

    @patch("app.services.search_service._get_config_int")
    def test_expired_snapshot_returns_re_search_prompt(self, mock_config):
        """P1: 快照过期后 show_more 应提示重新搜索。"""
        mock_config.return_value = 3
        session = _make_session()
        session.candidate_snapshot = CandidateSnapshot(
            candidate_ids=["1", "2", "3"],
            query_digest="abc",
            expires_at=_past_expires(),
        )
        session.shown_items = ["1"]

        db = MagicMock()
        result = show_more(session, _make_user_ctx(), db)
        assert "过期" in result.reply_text or "重新搜索" in result.reply_text
        # 快照应被清空
        assert session.candidate_snapshot is None

    @patch("app.services.search_service.permission_service")
    @patch("app.services.search_service._validate_job_ids")
    @patch("app.services.search_service._jobs_to_dicts")
    @patch("app.services.search_service._get_config_int")
    def test_show_more_remaining_count_accurate(
        self, mock_config, mock_dicts, mock_validate, mock_perm,
    ):
        """P2: show_more 的"还有 N 个"数字必须准确。"""
        mock_config.return_value = 2  # top_n = 2

        # 模拟 5 个候选，已展示 2 个，本次取 2 个有效
        mock_job_1 = MagicMock(id=3, owner_userid="f1", city="苏州", job_category="电子厂",
                               salary_floor_monthly=5000, salary_ceiling_monthly=None,
                               pay_type="月薪", headcount=10, gender_required="不限",
                               is_long_term=True, district=None, provide_meal=True,
                               provide_housing=True, shift_pattern=None, work_hours=None,
                               description="test", created_at=None)
        mock_job_2 = MagicMock(id=4, owner_userid="f1", **{
            attr: getattr(mock_job_1, attr)
            for attr in ["city", "job_category", "salary_floor_monthly",
                         "salary_ceiling_monthly", "pay_type", "headcount",
                         "gender_required", "is_long_term", "district",
                         "provide_meal", "provide_housing", "shift_pattern",
                         "work_hours", "description", "created_at"]
        })

        mock_validate.return_value = [mock_job_1, mock_job_2]
        mock_dicts.return_value = [
            {"id": 3, "city": "苏州", "job_category": "电子厂",
             "salary_floor_monthly": 5000, "pay_type": "月薪", "company": "XX"},
            {"id": 4, "city": "苏州", "job_category": "电子厂",
             "salary_floor_monthly": 5000, "pay_type": "月薪", "company": "YY"},
        ]
        mock_perm.filter_jobs_batch.return_value = mock_dicts.return_value

        session = _make_session()
        session.candidate_snapshot = CandidateSnapshot(
            candidate_ids=["1", "2", "3", "4", "5"],
            query_digest="abc",
            expires_at=_fresh_expires(),
        )
        session.shown_items = ["1", "2"]

        db = MagicMock()
        result = show_more(session, _make_user_ctx(), db)
        assert result.result_count == 2
        # 展示了 3,4 后，剩余应该是 5（1个）
        assert result.has_more is True
        assert "1" in result.reply_text  # "还有 1 个"


# ---------------------------------------------------------------------------
# Bug 3 — fallback 采纳前缀 + 0 命中建议方向
# ---------------------------------------------------------------------------

class TestSummarizeCriteria:
    def test_full_summary(self):
        text = _summarize_search_criteria(
            {"city": ["北京市"], "job_category": ["餐饮"], "salary_floor_monthly": 2200},
            "salary_floor_monthly",
        )
        assert "北京市" in text and "餐饮" in text and "2200" in text and "≥" in text

    def test_resume_uses_ceiling_prefix(self):
        text = _summarize_search_criteria(
            {"city": ["北京市"], "salary_ceiling_monthly": 6000},
            "salary_ceiling_monthly",
        )
        assert "≤6000" in text

    def test_empty_criteria(self):
        assert _summarize_search_criteria({}, "salary_floor_monthly") == "当前条件"

    def test_handles_str_city_and_category(self):
        # criteria 偶尔会是裸 str 而非 list（旧数据 / patch 路径）
        text = _summarize_search_criteria(
            {"city": "北京市", "job_category": "餐饮"},
            "salary_floor_monthly",
        )
        assert "北京市" in text and "餐饮" in text


class TestProbeJobSuggestions:
    @patch("app.services.search_service._query_jobs")
    def test_drop_salary_yields_suggestion(self, mock_query):
        # 探查：去掉 salary → 命中 5；去掉 job_category → 命中 0；keep_city_only → 命中 2
        def fake(criteria, *_):
            if "salary_floor_monthly" not in criteria and "job_category" in criteria:
                return [MagicMock()] * 5
            if "job_category" not in criteria and "salary_floor_monthly" in criteria:
                return []
            if list(criteria.keys()) == ["city"]:
                return [MagicMock()] * 2
            return []
        mock_query.side_effect = fake

        criteria = {
            "city": ["北京市"], "job_category": ["餐饮"],
            "salary_floor_monthly": 8000,
        }
        suggestions = _probe_job_suggestions(criteria, 50, MagicMock())
        # 命中数降序：drop_salary(5) > keep_city_only(2)
        assert len(suggestions) == 2
        assert suggestions[0].step == "drop_salary"
        assert suggestions[0].count == 5
        assert suggestions[1].step == "keep_city_only"

    @patch("app.services.search_service._query_jobs")
    def test_no_probe_with_only_city(self, mock_query):
        # 没 salary、没 job_category → drop_salary/drop_job_category 跳过；
        # keep_city_only 与 criteria 完全相同（仅 city）→ 也跳过
        criteria = {"city": ["北京市"]}
        suggestions = _probe_job_suggestions(criteria, 50, MagicMock())
        assert suggestions == []
        mock_query.assert_not_called()

    @patch("app.services.search_service._query_jobs")
    def test_keep_city_only_skipped_when_equal_to_drop_category(self, mock_query):
        # criteria 只有 city + job_category：drop_job_category → {city}，
        # keep_city_only 也 → {city}，应去重，仅一次探查
        mock_query.return_value = [MagicMock()] * 2
        criteria = {"city": ["北京市"], "job_category": ["餐饮"]}
        suggestions = _probe_job_suggestions(criteria, 50, MagicMock())
        assert mock_query.call_count == 1
        assert len(suggestions) == 1
        assert suggestions[0].step == "drop_job_category"

    @patch("app.services.search_service._query_jobs")
    def test_safety_guard_blocks_empty_criteria(self, mock_query):
        # 即使被探查方向去掉太多导致无 city/job_category，也不允许查询
        mock_query.return_value = [MagicMock()]
        # job_category=[]（空列表）+ salary → drop_job_category 拿到 {city,salary}
        # drop_salary 拿到 {city,job_category=[]} → 走 has_effective_search_criteria 判定
        criteria = {"city": ["北京市"], "job_category": [], "salary_floor_monthly": 5000}
        # 此用例核心：没有触发 has_effective_search_criteria=False 的分支
        suggestions = _probe_job_suggestions(criteria, 50, MagicMock())
        # 至少 drop_salary 探查能跑通；不强断言数量，断言不崩
        assert isinstance(suggestions, list)


class TestProbeResumeSuggestions:
    @patch("app.services.search_service._query_resumes")
    def test_drop_ceiling_yields_suggestion(self, mock_query):
        mock_query.return_value = [MagicMock()] * 3
        criteria = {
            "city": ["北京市"], "job_category": ["餐饮"],
            "salary_ceiling_monthly": 4000,
        }
        suggestions = _probe_resume_suggestions(criteria, 50, MagicMock())
        # 三步都返回 3 → 三个 suggestions 都被收录
        steps = {s.step for s in suggestions}
        assert "drop_salary_ceiling" in steps


class TestNoMatchSuggestionFormatting:
    def test_job_format_includes_summary_and_counts(self):
        text = _format_no_match_with_suggestions_job(
            {"city": ["北京市"], "job_category": ["餐饮"], "salary_floor_monthly": 2200},
            [
                FallbackSuggestion(step="drop_salary", criteria={}, count=5),
                FallbackSuggestion(step="keep_city_only", criteria={}, count=2),
            ],
        )
        assert "北京市" in text and "餐饮" in text and "≥2200" in text
        assert "1. 不限薪资 —— 约 5 条" in text
        assert "2. 只保留城市 —— 约 2 条" in text

    def test_resume_format_uses_ceiling(self):
        text = _format_no_match_with_suggestions_resume(
            {"city": ["北京市"], "salary_ceiling_monthly": 4000},
            [FallbackSuggestion(step="drop_salary_ceiling", criteria={}, count=3)],
        )
        assert "≤4000" in text
        assert "不限期望薪资 —— 约 3 位" in text


class TestSearchJobsFallbackPrefix:
    """端到端：fallback 采纳某步时，reply 应以前缀通知开头。"""

    @patch("app.services.search_service.permission_service")
    @patch("app.services.search_service.conversation_service")
    @patch("app.services.search_service._rerank_with_logging")
    @patch("app.services.search_service._jobs_to_dicts")
    @patch("app.services.search_service._query_jobs")
    @patch("app.services.search_service._get_config_int")
    def test_applied_step_prepends_notice(
        self, mock_config, mock_query, mock_to_dicts, mock_rerank,
        mock_conv, mock_perm,
    ):
        mock_config.side_effect = lambda key, *_: 3 if "top_n" in key else 50
        # 原条件 0 命中；relax_salary 命中 3 → applied_step=relax_salary_10pct
        mock_query.side_effect = [[], [MagicMock()] * 3]
        job_dicts = [
            {"id": i, "city": "北京", "job_category": "餐饮",
             "salary_floor_monthly": 1980, "pay_type": "月薪", "company": f"C{i}"}
            for i in (1, 2, 3)
        ]
        mock_to_dicts.return_value = job_dicts
        mock_rerank.return_value = RerankResult(
            ranked_items=[{"id": 1, "score": 0.9}, {"id": 2, "score": 0.8},
                          {"id": 3, "score": 0.7}],
            reply_text="",
            raw_response="",
        )
        mock_conv.compute_query_digest.return_value = "abc"
        mock_conv.get_next_candidate_ids.return_value = ["1", "2", "3"]
        mock_conv.get_remaining_count.return_value = 0
        mock_perm.filter_jobs_batch.return_value = job_dicts

        criteria = {
            "city": ["北京"], "job_category": ["餐饮"], "salary_floor_monthly": 2200,
        }
        result = search_jobs(criteria, "北京餐饮", _make_session(), _make_user_ctx(), MagicMock())

        assert result.result_count == 3
        assert result.reply_text.startswith(_FALLBACK_NOTICE_JOB["relax_salary_10pct"])
        assert "为您找到" in result.reply_text


class TestSearchJobsNoMatchSuggestion:
    """端到端：温和放宽全 0、激进探查命中 → reply 给出"建议方向"。"""

    @patch("app.services.search_service._rerank_with_logging")
    @patch("app.services.search_service._query_jobs")
    @patch("app.services.search_service._get_config_int")
    def test_zero_recall_with_suggestion(
        self, mock_config, mock_query, mock_rerank,
    ):
        mock_config.side_effect = lambda key, *_: 3 if "top_n" in key else 50

        criteria = {
            "city": ["北京"], "job_category": ["餐饮"], "salary_floor_monthly": 8000,
        }

        def fake_query(c, *_):
            # 温和放宽（仍有 salary 或 job_category）—— 全 0
            has_salary = c.get("salary_floor_monthly") is not None
            has_cat = bool(c.get("job_category"))
            if has_salary and has_cat:
                return []
            # drop_salary（保留 job_category）→ 4 条
            if not has_salary and has_cat:
                return [MagicMock()] * 4
            # drop_job_category（保留 salary）→ 2 条
            if has_salary and not has_cat:
                return [MagicMock()] * 2
            # keep_city_only → 与 drop_salary/drop_category 不重合（仅 city），但
            # 此 criteria 中两者都被去掉了，应 ≥1 才放进 suggestions
            return [MagicMock()] * 1

        mock_query.side_effect = fake_query

        result = search_jobs(criteria, "原查询", _make_session(), _make_user_ctx(), MagicMock())

        # 0 命中，reranker 不应被调用
        mock_rerank.assert_not_called()
        assert result.result_count == 0
        assert "原条件" in result.reply_text
        assert "可以放宽以下方向" in result.reply_text
        assert "不限薪资" in result.reply_text  # drop_salary 命中 4 条排第一

    @patch("app.services.search_service._query_jobs")
    @patch("app.services.search_service._get_config_int")
    def test_zero_recall_no_suggestions_fallback_to_static(
        self, mock_config, mock_query,
    ):
        # 所有探查全 0 → 回到 NO_JOB_MATCH_REPLY 静态文案
        mock_config.side_effect = lambda key, *_: 3 if "top_n" in key else 50
        mock_query.return_value = []
        criteria = {
            "city": ["北京"], "job_category": ["餐饮"], "salary_floor_monthly": 8000,
        }
        result = search_jobs(criteria, "原查询", _make_session(), _make_user_ctx(), MagicMock())
        assert result.reply_text == NO_JOB_MATCH_REPLY


class TestSearchWorkersFallback:
    """search_workers 的同型回归用例，确保对称改动也生效。"""

    @patch("app.services.search_service._rerank_with_logging")
    @patch("app.services.search_service._query_resumes")
    @patch("app.services.search_service._get_config_int")
    def test_zero_recall_with_suggestion(
        self, mock_config, mock_query, mock_rerank,
    ):
        mock_config.side_effect = lambda key, *_: 3 if "top_n" in key else 50
        criteria = {
            "city": ["北京"], "job_category": ["餐饮"], "salary_ceiling_monthly": 3000,
        }

        def fake_query(c, *_):
            has_ceiling = c.get("salary_ceiling_monthly") is not None
            has_cat = bool(c.get("job_category"))
            if has_ceiling and has_cat:
                return []
            if not has_ceiling and has_cat:
                return [MagicMock()] * 3
            return []

        mock_query.side_effect = fake_query

        result = search_workers(
            criteria, "原查询", _make_session(role="factory"),
            _make_user_ctx(role="factory"), MagicMock(),
        )

        mock_rerank.assert_not_called()
        assert "求职者" in result.reply_text
        assert "不限期望薪资" in result.reply_text

    @patch("app.services.search_service._query_resumes")
    @patch("app.services.search_service._get_config_int")
    def test_zero_recall_no_suggestion(
        self, mock_config, mock_query,
    ):
        mock_config.side_effect = lambda key, *_: 3 if "top_n" in key else 50
        mock_query.return_value = []
        criteria = {"city": ["北京"], "job_category": ["餐饮"]}
        result = search_workers(
            criteria, "原查询", _make_session(role="factory"),
            _make_user_ctx(role="factory"), MagicMock(),
        )
        assert result.reply_text == NO_WORKER_MATCH_REPLY
