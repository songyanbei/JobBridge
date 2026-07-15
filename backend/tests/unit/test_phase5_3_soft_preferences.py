"""Phase 5.3 软偏好排序单测。

phased-plan §5.3 验收：
- soft_preference_ranking_enabled=False 时 rerank 调用严格等价 5.2（不传 soft_*）
- True 时 reranker 收到 soft_preferences + ranking_weights，走 v2.1 prompt
- slot_schema.extract_soft_preferences 按权重表抽取
- format_soft_preferences_block 渲染
- 候选集（rerank 前）数量在开关前后完全相同（软偏好不影响硬过滤）
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.config import settings
from app.dialogue import slot_schema
from app.llm.prompts import (
    RERANK_USER_TEMPLATE,
    RERANK_USER_TEMPLATE_WITH_SOFT_PREF,
    format_soft_preferences_block,
)


# ---------------------------------------------------------------------------
# slot_schema.extract_soft_preferences
# ---------------------------------------------------------------------------


class TestExtractSoftPreferences:
    def test_extracts_provide_meal_with_weight(self):
        criteria = {"city": ["北京市"], "provide_meal": True}
        prefs, weights = slot_schema.extract_soft_preferences(criteria, frame="job_search")
        assert prefs == {"provide_meal": True}
        assert weights == {"provide_meal": 0.3}

    def test_extracts_multiple_soft_prefs(self):
        criteria = {
            "city": ["北京市"],
            "job_category": ["餐饮"],
            "provide_meal": True,
            "provide_housing": True,
            "shift_pattern": "日班",
        }
        prefs, weights = slot_schema.extract_soft_preferences(criteria, frame="job_search")
        assert set(prefs.keys()) == {"provide_meal", "provide_housing", "shift_pattern"}
        assert weights["provide_meal"] == 0.3
        assert weights["shift_pattern"] == 0.2

    def test_skips_hard_filter_fields(self):
        # city / job_category / salary_* 是硬过滤字段，不进 soft_preferences
        criteria = {
            "city": ["北京市"],
            "job_category": ["餐饮"],
            "salary_floor_monthly": 5000,
        }
        prefs, weights = slot_schema.extract_soft_preferences(criteria, frame="job_search")
        assert prefs == {}
        assert weights == {}

    def test_skips_none_values(self):
        criteria = {"provide_meal": None, "shift_pattern": "日班"}
        prefs, weights = slot_schema.extract_soft_preferences(criteria, frame="job_search")
        assert prefs == {"shift_pattern": "日班"}

    def test_empty_criteria(self):
        prefs, weights = slot_schema.extract_soft_preferences({}, frame="job_search")
        assert prefs == {}
        assert weights == {}

    def test_none_criteria(self):
        prefs, weights = slot_schema.extract_soft_preferences(None, frame="job_search")
        assert prefs == {}

    def test_candidate_search_uses_same_weights(self):
        """candidate_search frame 与 job_search 共享同一权重表。"""
        criteria = {"provide_meal": True}
        prefs_a, weights_a = slot_schema.extract_soft_preferences(
            criteria, frame="job_search",
        )
        prefs_b, weights_b = slot_schema.extract_soft_preferences(
            criteria, frame="candidate_search",
        )
        assert prefs_a == prefs_b
        assert weights_a == weights_b


# ---------------------------------------------------------------------------
# format_soft_preferences_block
# ---------------------------------------------------------------------------


class TestFormatSoftPreferencesBlock:
    def test_empty_returns_empty_string(self):
        assert format_soft_preferences_block({}, {}) == ""
        assert format_soft_preferences_block(None, None) == ""

    def test_renders_single_pref(self):
        block = format_soft_preferences_block(
            {"provide_meal": True},
            {"provide_meal": 0.3},
        )
        assert "provide_meal: True" in block
        assert "(权重 0.3)" in block

    def test_renders_multiple_prefs(self):
        block = format_soft_preferences_block(
            {"provide_meal": True, "shift_pattern": "日班"},
            {"provide_meal": 0.3, "shift_pattern": 0.2},
        )
        assert "provide_meal: True (权重 0.3)" in block
        assert "shift_pattern: 日班 (权重 0.2)" in block

    def test_no_weights_omits_weight_suffix(self):
        block = format_soft_preferences_block(
            {"provide_meal": True}, {},
        )
        assert "provide_meal: True" in block
        assert "权重" not in block


# ---------------------------------------------------------------------------
# search_service._extract_soft_prefs_for_rerank（受 settings 控制）
# ---------------------------------------------------------------------------


class TestExtractSoftPrefsForRerank:
    def test_disabled_returns_empty(self):
        from app.services.search_service import _extract_soft_prefs_for_rerank
        # 默认 soft_preference_ranking_enabled=False
        assert settings.soft_preference_ranking_enabled is False
        prefs, weights = _extract_soft_prefs_for_rerank(
            {"provide_meal": True}, "job_search",
        )
        assert prefs == {}
        assert weights == {}

    def test_enabled_extracts(self, monkeypatch):
        from app.services.search_service import _extract_soft_prefs_for_rerank
        from app.services.recommendation_experience_gate import (
            RecommendationExperienceFlags,
        )
        monkeypatch.setattr(settings, "soft_preference_ranking_enabled", True)
        prefs, weights = _extract_soft_prefs_for_rerank(
            {"city": ["北京市"], "provide_meal": True},
            "job_search",
            RecommendationExperienceFlags(soft_preference_ranking=True),
        )
        assert prefs == {"provide_meal": True}
        assert weights == {"provide_meal": 0.3}


# ---------------------------------------------------------------------------
# Reranker prompt v2.0 vs v2.1（向后兼容）
# ---------------------------------------------------------------------------


class TestRerankerPromptCompatibility:
    """phased-plan §5.3.4 验收 #5：mock provider 不实现 v2 时调用方仍能拿到
    v1 等价结果，无异常抛出（向后兼容）。"""

    def test_v2_0_template_unchanged(self):
        # 5.3 不能改 v2.0 模板（向后兼容）
        prompt = RERANK_USER_TEMPLATE.format(
            query="北京找餐饮",
            candidates="[]",
        )
        assert "北京找餐饮" in prompt
        assert "用户软偏好" not in prompt  # v2.0 没有软偏好块

    def test_v2_1_template_includes_soft_pref_block(self):
        prompt = RERANK_USER_TEMPLATE_WITH_SOFT_PREF.format(
            query="北京找餐饮",
            candidates="[]",
            soft_preferences_block="- provide_meal: True (权重 0.3)",
        )
        assert "用户软偏好" in prompt
        assert "provide_meal: True" in prompt
        assert "硬过滤" in prompt  # 强调软偏好仅影响顺序

    def _build_mock_resp(self):
        """构造类似 httpx.Response 的 mock 对象。"""
        resp = MagicMock()
        resp.json.return_value = {
            "choices": [{"message": {"content": '{"ranked_items":[],"reply_text":""}'}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        return resp

    def test_doubao_provider_no_soft_prefs_uses_v2_0(self):
        """soft_preferences=None 时 Doubao provider 严格走 v2.0 模板。"""
        from app.llm.providers.doubao import DoubaoReranker
        with patch("app.llm.providers.doubao.call_llm_api") as mock_call:
            mock_call.return_value = self._build_mock_resp()
            r = DoubaoReranker()
            r.rerank(
                query="test",
                candidates=[{"id": 1, "city": "北京市"}],
                role="worker",
                top_n=1,
                # soft_preferences=None（默认）
            )
            sent_payload = mock_call.call_args.kwargs["payload"]
            user_msg = sent_payload["messages"][1]["content"]
            assert "用户软偏好" not in user_msg

    def test_doubao_provider_with_soft_prefs_uses_v2_1(self):
        from app.llm.providers.doubao import DoubaoReranker
        with patch("app.llm.providers.doubao.call_llm_api") as mock_call:
            mock_call.return_value = self._build_mock_resp()
            r = DoubaoReranker()
            r.rerank(
                query="test",
                candidates=[{"id": 1, "city": "北京市"}],
                role="worker",
                top_n=1,
                soft_preferences={"provide_meal": True},
                ranking_weights={"provide_meal": 0.3},
            )
            sent_payload = mock_call.call_args.kwargs["payload"]
            user_msg = sent_payload["messages"][1]["content"]
            assert "用户软偏好" in user_msg
            assert "provide_meal: True" in user_msg


# ---------------------------------------------------------------------------
# search_service rerank 调用：开关控制（核心验收）
# ---------------------------------------------------------------------------


class TestSearchServiceRerankSoftPrefIntegration:
    """phased-plan §5.3.4 验收 #1：disabled 时 rerank 调用参数与 5.2 等价。"""

    @patch("app.services.search_service.get_reranker")
    def test_disabled_does_not_pass_soft_prefs(self, mock_factory, monkeypatch):
        from app.llm.base import RerankResult
        from app.services.search_service import _rerank_with_logging
        mock_reranker = MagicMock()
        mock_reranker.rerank.return_value = RerankResult(
            ranked_items=[], reply_text="",
        )
        mock_factory.return_value = mock_reranker

        # disabled 默认
        _rerank_with_logging(
            query="x", candidates=[{"id": 1}], role="worker",
            top_n=3, call_site="test",
            soft_preferences=None,
            ranking_weights=None,
        )
        # rerank 调用时不应传 soft_preferences / ranking_weights
        kwargs = mock_reranker.rerank.call_args.kwargs
        assert "soft_preferences" not in kwargs
        assert "ranking_weights" not in kwargs

    @patch("app.services.search_service.get_reranker")
    def test_enabled_passes_soft_prefs(self, mock_factory):
        from app.llm.base import RerankResult
        from app.services.search_service import _rerank_with_logging
        mock_reranker = MagicMock()
        mock_reranker.rerank.return_value = RerankResult(
            ranked_items=[], reply_text="",
        )
        mock_factory.return_value = mock_reranker

        _rerank_with_logging(
            query="x", candidates=[{"id": 1}], role="worker",
            top_n=3, call_site="test",
            soft_preferences={"provide_meal": True},
            ranking_weights={"provide_meal": 0.3},
        )
        kwargs = mock_reranker.rerank.call_args.kwargs
        assert kwargs["soft_preferences"] == {"provide_meal": True}
        assert kwargs["ranking_weights"] == {"provide_meal": 0.3}
