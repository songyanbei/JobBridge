"""通义千问 (Qwen) LLM Provider。

使用 OpenAI 兼容 API 格式，通过 httpx 同步调用。
"""
import logging

import httpx

from app.config import settings
from app.core.exceptions import LLMError, LLMTimeout
from app.llm.base import IntentExtractor, IntentResult, Reranker, RerankResult
from app.llm.prompts import (
    INTENT_SYSTEM_PROMPT,
    INTENT_USER_TEMPLATE,
    RERANK_SYSTEM_PROMPT,
    RERANK_USER_TEMPLATE,
)
from app.llm.providers._base import (
    call_llm_api,
    format_candidates,
    format_criteria,
    format_history,
    parse_intent_response,
    parse_rerank_response,
)

logger = logging.getLogger(__name__)


def _build_headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.llm_api_key}",
        "Content-Type": "application/json",
    }


def _chat_url() -> str:
    base = settings.llm_api_base.rstrip("/")
    return f"{base}/chat/completions"


def _extract_content(resp_json: dict) -> str:
    """从 OpenAI 兼容响应中提取 assistant content。"""
    try:
        return resp_json["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""


class QwenIntentExtractor(IntentExtractor):
    """基于通义千问的意图抽取实现。"""

    def extract(
        self,
        text: str,
        role: str,
        history: list[dict] | None = None,
        current_criteria: dict | None = None,
    ) -> IntentResult:
        system_prompt = INTENT_SYSTEM_PROMPT.format(
            role=role,
            history=format_history(history),
            current_criteria=format_criteria(current_criteria),
        )
        user_prompt = INTENT_USER_TEMPLATE.format(text=text)

        payload = {
            "model": settings.llm_intent_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
        }

        try:
            resp = call_llm_api(
                url=_chat_url(),
                headers=_build_headers(),
                payload=payload,
                timeout=settings.llm_timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.ConnectError):
            raise LLMTimeout()
        except httpx.HTTPStatusError as exc:
            raise LLMError(f"Qwen API HTTP error: {exc.response.status_code}")

        raw = _extract_content(resp.json())
        return parse_intent_response(raw)


class QwenReranker(Reranker):
    """基于通义千问的重排实现。"""

    def rerank(
        self,
        query: str,
        candidates: list[dict],
        role: str,
        top_n: int = 3,
    ) -> RerankResult:
        system_prompt = RERANK_SYSTEM_PROMPT.format(
            role=role,
            top_n=top_n,
        )
        user_prompt = RERANK_USER_TEMPLATE.format(
            query=query,
            candidates=format_candidates(candidates),
        )

        payload = {
            "model": settings.llm_reranker_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
        }

        try:
            resp = call_llm_api(
                url=_chat_url(),
                headers=_build_headers(),
                payload=payload,
                timeout=settings.llm_timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.ConnectError):
            raise LLMTimeout()
        except httpx.HTTPStatusError as exc:
            raise LLMError(f"Qwen API HTTP error: {exc.response.status_code}")

        raw = _extract_content(resp.json())
        return parse_rerank_response(raw)
