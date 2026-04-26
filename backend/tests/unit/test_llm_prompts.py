"""Prompt 骨架测试：常量存在、版本标记、token 预算、JSON 契约、字段对齐。"""
import pytest

from app.llm import prompts
from app.llm.base import IntentResult, RerankResult


class TestIntentPrompt:
    """Intent prompt 常量测试。"""

    def test_prompt_constant_exists(self):
        assert hasattr(prompts, "INTENT_SYSTEM_PROMPT")
        assert len(prompts.INTENT_SYSTEM_PROMPT) > 0

    def test_user_template_exists(self):
        assert hasattr(prompts, "INTENT_USER_TEMPLATE")
        assert "{text}" in prompts.INTENT_USER_TEMPLATE

    def test_version_tag(self):
        # Stage B 2026-04-26: bump to v2.1，prompt 加 closed-enum job_category + few-shot 同义词归并
        assert prompts.PROMPT_VERSION == "v2.1"
        assert prompts.PROMPT_DATE == "2026-04-26"

    def test_token_budget_constants(self):
        assert prompts.INTENT_INPUT_TOKEN_BUDGET == 2000
        assert prompts.INTENT_OUTPUT_TOKEN_BUDGET == 500

    def test_strict_json_constraint(self):
        prompt = prompts.INTENT_SYSTEM_PROMPT
        assert "JSON" in prompt
        assert "markdown" in prompt.lower() or "code block" in prompt.lower() or "code fence" in prompt.lower()

    def test_field_alignment_with_intent_result(self):
        """prompt 中提到的字段名必须与 IntentResult 对齐。"""
        prompt = prompts.INTENT_SYSTEM_PROMPT
        for field_name in ["intent", "structured_data", "criteria_patch", "missing_fields", "confidence"]:
            assert field_name in prompt, f"Field '{field_name}' not found in INTENT_SYSTEM_PROMPT"

    def test_fallback_rules_present(self):
        prompt = prompts.INTENT_SYSTEM_PROMPT
        assert "chitchat" in prompt
        assert "fallback" in prompt.lower() or "兜底" in prompt

    def test_prompt_has_role_placeholder(self):
        assert "{role}" in prompts.INTENT_SYSTEM_PROMPT

    def test_prompt_has_history_placeholder(self):
        assert "{history}" in prompts.INTENT_SYSTEM_PROMPT

    def test_prompt_has_criteria_placeholder(self):
        assert "{current_criteria}" in prompts.INTENT_SYSTEM_PROMPT

    def test_valid_intent_values_listed(self):
        prompt = prompts.INTENT_SYSTEM_PROMPT
        for intent in ["upload_job", "upload_resume", "search_job", "search_worker",
                        "upload_and_search", "follow_up", "show_more", "command", "chitchat"]:
            assert intent in prompt, f"Intent value '{intent}' not listed in prompt"


class TestRerankPrompt:
    """Rerank prompt 常量测试。"""

    def test_prompt_constant_exists(self):
        assert hasattr(prompts, "RERANK_SYSTEM_PROMPT")
        assert len(prompts.RERANK_SYSTEM_PROMPT) > 0

    def test_user_template_exists(self):
        assert hasattr(prompts, "RERANK_USER_TEMPLATE")
        assert "{query}" in prompts.RERANK_USER_TEMPLATE
        assert "{candidates}" in prompts.RERANK_USER_TEMPLATE

    def test_token_budget_constants(self):
        assert prompts.RERANK_INPUT_TOKEN_BUDGET == 4000
        assert prompts.RERANK_OUTPUT_TOKEN_BUDGET == 1000

    def test_strict_json_constraint(self):
        prompt = prompts.RERANK_SYSTEM_PROMPT
        assert "JSON" in prompt

    def test_field_alignment_with_rerank_result(self):
        prompt = prompts.RERANK_SYSTEM_PROMPT
        for field_name in ["ranked_items", "reply_text"]:
            assert field_name in prompt, f"Field '{field_name}' not found in RERANK_SYSTEM_PROMPT"

    def test_prompt_has_role_placeholder(self):
        assert "{role}" in prompts.RERANK_SYSTEM_PROMPT

    def test_prompt_has_top_n_placeholder(self):
        assert "{top_n}" in prompts.RERANK_SYSTEM_PROMPT

    def test_prompt_not_scattered_in_providers(self):
        """Prompt 不应散落在 provider 文件中。"""
        import app.llm.providers.qwen as qwen_mod
        import app.llm.providers.doubao as doubao_mod
        import inspect

        for mod in [qwen_mod, doubao_mod]:
            src = inspect.getsource(mod)
            # provider 中不应有多行 prompt 定义
            assert "你是一个" not in src, f"Prompt text found in {mod.__name__}"
