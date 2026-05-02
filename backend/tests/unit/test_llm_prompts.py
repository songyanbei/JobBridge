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
        # 阶段四 PR3 2026-05-02: bump to v2.7，criteria_patch 收口 — prompt 明确字段
        # 已废弃，所有 intent 输出 [];  v2 派生路径由 reducer + slot_schema 接管字段
        # 裁决，不再消费 op 语义；legacy 路径仍兼容空 patch 走 merge_criteria_patch no-op。
        assert prompts.PROMPT_VERSION == "v2.7"
        assert prompts.INTENT_PROMPT_VERSION == "v2.7"
        # PROMPT_VERSION 必须等于 INTENT_PROMPT_VERSION（一次 prompt 修订一组版本号）
        assert prompts.PROMPT_VERSION == prompts.INTENT_PROMPT_VERSION
        assert prompts.PROMPT_DATE == "2026-05-02"

    def test_intent_service_version_aliases_prompts(self):
        """intent_service.INTENT_PROMPT_VERSION 必须从 prompts 单一来源取，
        防止两份常量回到 v2.1 vs v2.7 不一致状态（reviewer P3）。"""
        from app.services import intent_service
        assert intent_service.INTENT_PROMPT_VERSION is prompts.INTENT_PROMPT_VERSION

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
