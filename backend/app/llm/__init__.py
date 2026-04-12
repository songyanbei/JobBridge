"""LLM 抽象层工厂（对应方案 §4.3）。

业务层统一通过 get_intent_extractor() / get_reranker() 获取实例，
不允许直接 import 具体 provider 类。
"""
from app.config import settings
from app.llm.base import IntentExtractor, Reranker

# provider 注册表：provider 名称 -> (IntentExtractor 类, Reranker 类)
_PROVIDER_REGISTRY: dict[str, tuple[type[IntentExtractor], type[Reranker]]] = {}


def _ensure_registry() -> None:
    """延迟注册 provider，避免 import 环路。"""
    if _PROVIDER_REGISTRY:
        return

    from app.llm.providers.qwen import QwenIntentExtractor, QwenReranker
    from app.llm.providers.doubao import DoubaoIntentExtractor, DoubaoReranker

    _PROVIDER_REGISTRY["qwen"] = (QwenIntentExtractor, QwenReranker)
    _PROVIDER_REGISTRY["doubao"] = (DoubaoIntentExtractor, DoubaoReranker)


def get_intent_extractor(provider: str | None = None) -> IntentExtractor:
    """获取意图抽取器实例。

    Args:
        provider: 指定 provider 名称，为 None 时读取 settings.llm_provider。

    Raises:
        ValueError: 未知 provider。
    """
    _ensure_registry()
    name = provider or settings.llm_provider
    entry = _PROVIDER_REGISTRY.get(name)
    if entry is None:
        raise ValueError(
            f"Unknown LLM provider: '{name}'. "
            f"Available: {sorted(_PROVIDER_REGISTRY.keys())}"
        )
    extractor_cls, _ = entry
    return extractor_cls()


def get_reranker(provider: str | None = None) -> Reranker:
    """获取重排器实例。

    Args:
        provider: 指定 provider 名称，为 None 时读取 settings.llm_provider。

    Raises:
        ValueError: 未知 provider。
    """
    _ensure_registry()
    name = provider or settings.llm_provider
    entry = _PROVIDER_REGISTRY.get(name)
    if entry is None:
        raise ValueError(
            f"Unknown LLM provider: '{name}'. "
            f"Available: {sorted(_PROVIDER_REGISTRY.keys())}"
        )
    _, reranker_cls = entry
    return reranker_cls()
