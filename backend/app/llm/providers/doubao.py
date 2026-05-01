"""豆包 (Doubao) LLM Provider。

豆包 API 同样兼容 OpenAI chat/completions 格式，通过 httpx 同步调用。
Phase 2 以结构骨架 + mock 测试可过为交付标准，不以真实 API 联调成功作为阻塞条件。
"""
import logging

import httpx

from app.config import settings
from app.core.exceptions import LLMError, LLMParseError, LLMTimeout
from app.llm.base import (
    DialogueParseResult,
    IntentExtractor,
    IntentResult,
    Reranker,
    RerankResult,
)
from app.llm.prompts import (
    DIALOGUE_PARSE_PROMPT_V2,
    DIALOGUE_USER_TEMPLATE,
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
    format_session_hint,
    parse_dialogue_response,
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


def _extract_usage(resp_json: dict) -> tuple[int | None, int | None]:
    """从 OpenAI 兼容响应中提取 token 用量（Phase 7 llm_call 日志字段）。"""
    usage = resp_json.get("usage") or {}
    in_tok = usage.get("prompt_tokens")
    out_tok = usage.get("completion_tokens")
    try:
        in_tok = int(in_tok) if in_tok is not None else None
    except (TypeError, ValueError):
        in_tok = None
    try:
        out_tok = int(out_tok) if out_tok is not None else None
    except (TypeError, ValueError):
        out_tok = None
    return in_tok, out_tok


class DoubaoIntentExtractor(IntentExtractor):
    """基于豆包的意图抽取实现。"""

    def extract(
        self,
        text: str,
        role: str,
        history: list[dict] | None = None,
        current_criteria: dict | None = None,
        session_hint: dict | None = None,
    ) -> IntentResult:
        system_prompt = INTENT_SYSTEM_PROMPT.format(
            role=role,
            history=format_history(history),
            current_criteria=format_criteria(current_criteria),
            session_hint=format_session_hint(session_hint),
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

        resp_json = resp.json()
        raw = _extract_content(resp_json)
        # Phase 7：usage 先提再 parse，parse 失败时把 token 挂到异常上
        # 让上层 log_event 仍能记录真实的 input_tokens / output_tokens。
        in_tok, out_tok = _extract_usage(resp_json)
        try:
            result = parse_intent_response(raw)
        except LLMParseError as exc:
            exc.input_tokens = in_tok
            exc.output_tokens = out_tok
            raise
        result.input_tokens = in_tok
        result.output_tokens = out_tok
        return result

    def extract_dialogue(
        self,
        text: str,
        role: str,
        history: list[dict] | None = None,
        current_criteria: dict | None = None,
        session_hint: dict | None = None,
    ) -> DialogueParseResult:
        """阶段二：解析为 DialogueParseResult（dialogue-intent-extraction-phased-plan §2）。"""
        system_prompt = DIALOGUE_PARSE_PROMPT_V2.format(
            role=role,
            history=format_history(history),
            current_criteria=format_criteria(current_criteria),
            session_hint=format_session_hint(session_hint),
        )
        user_prompt = DIALOGUE_USER_TEMPLATE.format(text=text)

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

        resp_json = resp.json()
        raw = _extract_content(resp_json)
        in_tok, out_tok = _extract_usage(resp_json)
        try:
            result = parse_dialogue_response(raw)
        except LLMParseError as exc:
            exc.input_tokens = in_tok
            exc.output_tokens = out_tok
            raise
        result.input_tokens = in_tok
        result.output_tokens = out_tok
        return result


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

        resp_json = resp.json()
        raw = _extract_content(resp_json)
        # 同 intent 路径：usage 先提再 parse，parse 失败时把 token 挂到异常
        in_tok, out_tok = _extract_usage(resp_json)
        try:
            result = parse_rerank_response(raw)
        except LLMParseError as exc:
            exc.input_tokens = in_tok
            exc.output_tokens = out_tok
            raise
        result.input_tokens = in_tok
        result.output_tokens = out_tok
        return result
