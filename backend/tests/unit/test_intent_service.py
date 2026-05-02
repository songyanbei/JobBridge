"""intent_service 单元测试。"""
from unittest.mock import MagicMock, patch

import pytest

from app.llm.base import IntentResult
from app.services.intent_service import (
    _apply_worker_intent_guard,
    _match_command,
    _match_show_more,
    _sanitize_common,
    _sanitize_intent_result,
    classify_intent,
)


class TestMatchCommand:
    def test_exact_slash_command(self):
        assert _match_command("/帮助") == ("help", "")
        assert _match_command("/重新找") == ("reset_search", "")
        assert _match_command("/删除我的信息") == ("delete_my_data", "")
        assert _match_command("/找岗位") == ("switch_to_job", "")
        assert _match_command("/找工人") == ("switch_to_worker", "")
        assert _match_command("/我的状态") == ("my_status", "")

    def test_alias_matching(self):
        assert _match_command("帮助") == ("help", "")
        assert _match_command("重来") == ("reset_search", "")
        assert _match_command("注销") == ("delete_my_data", "")
        assert _match_command("转人工") == ("human_agent", "")

    def test_no_match(self):
        assert _match_command("苏州找电子厂") is None
        assert _match_command("你好") is None
        assert _match_command("") is None

    def test_phase4_new_commands(self):
        assert _match_command("/续期") == ("renew_job", "")
        assert _match_command("/下架") == ("delist_job", "")
        assert _match_command("/招满了") == ("filled_job", "")
        assert _match_command("招满了") == ("filled_job", "")
        assert _match_command("先不招了") == ("delist_job", "")

    def test_command_with_space_args(self):
        assert _match_command("/续期 15") == ("renew_job", "15")
        assert _match_command("/续期 30") == ("renew_job", "30")

    def test_command_with_sticky_args(self):
        assert _match_command("续15天") == ("renew_job", "15天")
        assert _match_command("续30天") == ("renew_job", "30天")

    def test_p1_1_loose_xu_prefix_does_not_match_unrelated_words(self):
        """P1-1：'续' 前缀必须紧跟数字，否则不能命中 renew_job。
        否则 '续约一下那个岗位'、'续保'、'续杯' 会被误识别为续期命令。"""
        assert _match_command("续约一下那个岗位") is None
        assert _match_command("续保") is None
        assert _match_command("续杯") is None
        assert _match_command("续订") is None
        # 正常数字紧跟仍命中
        assert _match_command("续15") == ("renew_job", "15")
        assert _match_command("续30天") == ("renew_job", "30天")


class TestMatchShowMore:
    def test_show_more_synonyms(self):
        assert _match_show_more("更多") is True
        assert _match_show_more("换一批") is True
        assert _match_show_more("还有吗") is True
        assert _match_show_more("看更多岗位") is True

    def test_not_show_more(self):
        assert _match_show_more("苏州找工作") is False
        assert _match_show_more("你好") is False


class TestSanitizeIntentResult:
    def test_drops_unknown_structured_data_key(self):
        result = IntentResult(
            intent="search_job",
            structured_data={"city": ["苏州市"], "unknown_field": "bad"},
            confidence=0.9,
        )
        sanitized = _sanitize_intent_result(result, "worker")
        assert "city" in sanitized.structured_data
        assert "unknown_field" not in sanitized.structured_data

    def test_drops_invalid_patch_op(self):
        result = IntentResult(
            intent="follow_up",
            criteria_patch=[
                {"op": "update", "field": "city", "value": ["苏州市"]},
                {"op": "set", "field": "city", "value": ["昆山市"]},  # invalid op
            ],
            confidence=0.8,
        )
        sanitized = _sanitize_intent_result(result, "worker")
        assert len(sanitized.criteria_patch) == 1
        assert sanitized.criteria_patch[0]["op"] == "update"

    def test_drops_unknown_patch_field(self):
        result = IntentResult(
            intent="follow_up",
            criteria_patch=[
                {"op": "update", "field": "fake_field", "value": 123},
            ],
            confidence=0.8,
        )
        sanitized = _sanitize_intent_result(result, "worker")
        assert len(sanitized.criteria_patch) == 0

    def test_filters_sensitive_missing_fields(self):
        result = IntentResult(
            intent="upload_resume",
            missing_fields=["gender", "age", "ethnicity", "has_tattoo"],
            confidence=0.7,
        )
        sanitized = _sanitize_intent_result(result, "worker")
        assert "gender" in sanitized.missing_fields
        assert "age" in sanitized.missing_fields
        assert "ethnicity" not in sanitized.missing_fields
        assert "has_tattoo" not in sanitized.missing_fields

    def test_only_allowed_required_fields(self):
        result = IntentResult(
            intent="upload_job",
            missing_fields=["city", "description", "unknown"],
            confidence=0.7,
        )
        sanitized = _sanitize_intent_result(result, "factory")
        assert "city" in sanitized.missing_fields
        assert "description" not in sanitized.missing_fields
        assert "unknown" not in sanitized.missing_fields


class TestClassifyIntent:
    def test_command_priority_over_llm(self):
        result = classify_intent("/帮助", "worker")
        assert result.intent == "command"
        assert result.structured_data["command"] == "help"
        assert result.confidence == 1.0

    def test_show_more_priority(self):
        result = classify_intent("更多", "worker")
        assert result.intent == "show_more"
        assert result.confidence == 1.0

    @patch("app.services.intent_service.get_intent_extractor")
    def test_llm_fallback(self, mock_factory):
        mock_extractor = MagicMock()
        mock_extractor.extract.return_value = IntentResult(
            intent="search_job",
            structured_data={"city": ["苏州市"]},
            confidence=0.85,
        )
        mock_factory.return_value = mock_extractor

        result = classify_intent("苏州找电子厂", "worker")
        assert result.intent == "search_job"
        mock_extractor.extract.assert_called_once()


# ---------------------------------------------------------------------------
# Phase 4 (PR1)：sanitize 拆分 — worker 护栏 vs 字段清洗的结构化隔离
# ---------------------------------------------------------------------------


class TestPhase4SanitizeSplit:
    """验证 _sanitize_common 与 _apply_worker_intent_guard 拆分后的边界。

    阶段 4 (PR1)：worker 搜索护栏从 _sanitize_intent_result 抽离为独立函数，
    v2 主路径不会触发护栏；legacy 路径继续通过 _sanitize_intent_result 包装器
    或 _classify_intent_legacy 显式两步调用保留护栏。
    """

    def test_sanitize_common_does_not_apply_worker_guard(self):
        """_sanitize_common 单独调用时不会把 upload_job 纠回 search_job（v2 路径语义）。

        关键不变量：v2 主路径若调本函数，worker + search 信号 + upload_job 文本
        不会被静默纠正；intent 保留 LLM 原值，由 reducer 与 schema 接管裁决。
        """
        result = IntentResult(
            intent="upload_job",
            structured_data={"city": "西安市", "job_category": "餐饮"},
            confidence=0.8,
        )
        sanitized = _sanitize_common(result, "worker")
        # v2 路径不依赖 worker 护栏：intent 保持 LLM 原值
        assert sanitized.intent == "upload_job"

    def test_apply_worker_intent_guard_corrects_in_isolation(self):
        """_apply_worker_intent_guard 单独调用时按判据纠正 intent 并清空 missing。"""
        result = IntentResult(
            intent="upload_job",
            structured_data={"city": "西安市"},
            missing_fields=["pay_type", "headcount"],
            confidence=0.8,
        )
        guarded = _apply_worker_intent_guard(
            result, role="worker", raw_text="西安想找个饭店服务员的工作",
        )
        assert guarded.intent == "search_job"
        assert guarded.missing_fields == []

    def test_apply_worker_intent_guard_no_op_for_factory(self):
        """factory 角色不触发 worker 护栏，intent 不变。"""
        result = IntentResult(
            intent="upload_job",
            structured_data={"city": "苏州市"},
            confidence=0.9,
        )
        guarded = _apply_worker_intent_guard(
            result, role="factory", raw_text="想找人来做",
        )
        assert guarded.intent == "upload_job"

    def test_legacy_wrapper_still_applies_worker_guard(self):
        """_sanitize_intent_result 包装器在 legacy 路径上仍触发护栏（向后兼容）。"""
        result = IntentResult(
            intent="upload_job",
            structured_data={"city": "西安市", "job_category": "餐饮"},
            missing_fields=["pay_type", "headcount"],
            confidence=0.8,
        )
        sanitized = _sanitize_intent_result(
            result, role="worker", raw_text="西安想找个饭店服务员的工作",
        )
        # legacy 包装器 = worker guard + sanitize_common
        assert sanitized.intent == "search_job"
        assert sanitized.missing_fields == []
        # 城市 / 工种被搜索分支强制为 list（与 Phase 1 测试断言一致）
        assert sanitized.structured_data["city"] == ["西安市"]
        assert sanitized.structured_data["job_category"] == ["餐饮"]
