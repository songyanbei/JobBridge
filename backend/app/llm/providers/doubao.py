"""豆包 (Doubao) LLM Provider。

豆包 API 同样兼容 OpenAI chat/completions 格式，通过 httpx 同步调用。
Phase 2 以结构骨架 + mock 测试可过为交付标准，不以真实 API 联调成功作为阻塞条件。
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


class DoubaoIntentExtractor(IntentExtractor):
    """基于豆包的意图抽取实现。"""

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
            raise LLMError(f"Doubao API HTTP error: {exc.response.status_code}")

        raw = _extract_content(resp.json())
        return parse_intent_response(raw)


class DoubaoReranker(Reranker):
    """基于豆包的重排实现。"""

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
            raise LLMError(f"Doubao API HTTP error: {exc.response.status_code}")

        raw = _extract_content(resp.json())
        return parse_rerank_response(raw)
