"""search_service 单元测试。"""
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta, timezone

import pytest

from app.llm.base import RerankResult
from app.schemas.conversation import CandidateSnapshot, SessionState
from app.services.search_service import (
    SearchResult,
    _format_job_results,
    _format_resume_results,
    _is_job_search,
    search_jobs,
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
