"""LLM 工厂测试：provider 切换、未知 provider 报错。"""
import pytest
from unittest.mock import patch

from app.llm import get_intent_extractor, get_reranker, _PROVIDER_REGISTRY
from app.llm.base import IntentExtractor, Reranker
from app.llm.providers.qwen import QwenIntentExtractor, QwenReranker
from app.llm.providers.doubao import DoubaoIntentExtractor, DoubaoReranker


@pytest.fixture(autouse=True)
def clear_registry():
    """每个测试前清空注册表，确保 _ensure_registry 重新执行。"""
    _PROVIDER_REGISTRY.clear()
    yield
    _PROVIDER_REGISTRY.clear()


class TestGetIntentExtractor:

    def test_default_provider_qwen(self):
        with patch("app.llm.settings") as mock_settings:
            mock_settings.llm_provider = "qwen"
            extractor = get_intent_extractor()
            assert isinstance(extractor, QwenIntentExtractor)
            assert isinstance(extractor, IntentExtractor)

    def test_explicit_provider_doubao(self):
        extractor = get_intent_extractor(provider="doubao")
        assert isinstance(extractor, DoubaoIntentExtractor)
        assert isinstance(extractor, IntentExtractor)

    def test_explicit_provider_qwen(self):
        extractor = get_intent_extractor(provider="qwen")
        assert isinstance(extractor, QwenIntentExtractor)

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown LLM provider"):
            get_intent_extractor(provider="nonexistent")

    def test_unknown_provider_error_lists_available(self):
        with pytest.raises(ValueError, match="Available"):
            get_intent_extractor(provider="openai")


class TestGetReranker:

    def test_default_provider_qwen(self):
        with patch("app.llm.settings") as mock_settings:
            mock_settings.llm_provider = "qwen"
            reranker = get_reranker()
            assert isinstance(reranker, QwenReranker)
            assert isinstance(reranker, Reranker)

    def test_explicit_provider_doubao(self):
        reranker = get_reranker(provider="doubao")
        assert isinstance(reranker, DoubaoReranker)
        assert isinstance(reranker, Reranker)

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown LLM provider"):
            get_reranker(provider="nonexistent")


class TestProviderSwitching:
    """验证可通过工厂自由切换 provider。"""

    def test_switch_between_providers(self):
        qwen_ext = get_intent_extractor(provider="qwen")
        doubao_ext = get_intent_extractor(provider="doubao")
        assert type(qwen_ext) is not type(doubao_ext)

    def test_service_layer_does_not_need_direct_import(self):
        """验证业务层可以只 import 工厂函数和抽象类。"""
        from app.llm import get_intent_extractor, get_reranker
        from app.llm.base import IntentExtractor, Reranker
        ext = get_intent_extractor(provider="qwen")
        rnk = get_reranker(provider="qwen")
        assert isinstance(ext, IntentExtractor)
        assert isinstance(rnk, Reranker)
