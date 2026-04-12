"""LLM 相关 DTO。

IntentResult 和 RerankResult 已在 app.llm.base 中定义，
此处做统一导出，方便 service 层和 API 层引用。
"""
from app.llm.base import IntentResult, RerankResult

__all__ = ["IntentResult", "RerankResult"]
