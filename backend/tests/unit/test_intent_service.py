"""intent_service 单元测试。"""
from unittest.mock import MagicMock, patch

import pytest

from app.llm.base import IntentResult
from app.services.intent_service import (
    _match_command,
    _match_show_more,
    _sanitize_intent_result,
    classify_intent,
)


class TestMatchCommand:
    def test_exact_slash_command(self):
        assert _match_command("/帮助") == "help"
        assert _match_command("/重新找") == "reset_search"
        assert _match_command("/删除我的信息") == "delete_my_data"
        assert _match_command("/找岗位") == "switch_to_job"
        assert _match_command("/找工人") == "switch_to_worker"
        assert _match_command("/我的状态") == "my_status"

    def test_alias_matching(self):
        assert _match_command("帮助") == "help"
        assert _match_command("重来") == "reset_search"
        assert _match_command("注销") == "delete_my_data"
        assert _match_command("转人工") == "human_agent"

    def test_no_match(self):
        assert _match_command("苏州找电子厂") is None
        assert _match_command("你好") is None
        assert _match_command("") is None


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
