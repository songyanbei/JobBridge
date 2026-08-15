"""豆包 (Doubao) LLM Provider。

豆包 API 同样兼容 OpenAI chat/completions 格式。legacy 路径走 httpx 同步调用；
shadow 双算走 ``arerank()`` 的真异步路径（共享 AsyncClient + 绝对 deadline，§11.5）。
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
    LLMCallPolicy,
    Reranker,
    RerankResult,
)
from app.llm.prompts import (
    DIALOGUE_USER_TEMPLATE,
    INTENT_SYSTEM_PROMPT,
    INTENT_USER_TEMPLATE,
    get_dialogue_parse_prompt_v2,
)
from app.llm.providers._base import (
    build_rerank_payload,
    call_llm_api,
    call_llm_api_async,
    extract_chat_content as _extract_content,
    extract_chat_usage as _extract_usage,
    finalize_rerank_response,
    format_criteria,
    format_history,
    format_session_hint,
    parse_dialogue_response,
    parse_intent_response,
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
                # 不传 call_policy / timeout：意图抽取继续用原配置（§11.5），
                # 由 _base 解析成 legacy 的 llm_timeout_seconds + 一次重试。
            )
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            error = LLMTimeout()
            error.llm_retry_count = int(getattr(exc, "llm_retry_count", 0) or 0)
            raise error from exc
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
        system_prompt = get_dialogue_parse_prompt_v2().format(
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
                # 不传 call_policy / timeout：意图抽取继续用原配置（§11.5），
                # 由 _base 解析成 legacy 的 llm_timeout_seconds + 一次重试。
            )
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            error = LLMTimeout()
            error.llm_retry_count = int(getattr(exc, "llm_retry_count", 0) or 0)
            raise error from exc
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
    """基于豆包的重排实现。

    与 QwenReranker 共享同一份 payload 构造与响应解析（``_base``），
    ``call_policy`` 原样透传，provider 内不重新覆盖成全局 30 秒（§11.5）。
    """

    def rerank(
        self,
        query: str,
        candidates: list[dict],
        role: str,
        top_n: int = 3,
        *,
        soft_preferences: dict | None = None,
        ranking_weights: dict[str, float] | None = None,
        call_policy: LLMCallPolicy | None = None,
    ) -> RerankResult:
        payload = build_rerank_payload(
            query=query,
            candidates=candidates,
            role=role,
            top_n=top_n,
            soft_preferences=soft_preferences,
            ranking_weights=ranking_weights,
        )

        try:
            resp = call_llm_api(
                url=_chat_url(),
                headers=_build_headers(),
                payload=payload,
                call_policy=call_policy,
            )
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            error = LLMTimeout()
            error.llm_retry_count = int(getattr(exc, "llm_retry_count", 0) or 0)
            raise error from exc
        except httpx.HTTPStatusError as exc:
            raise LLMError(f"Doubao API HTTP error: {exc.response.status_code}")

        result = finalize_rerank_response(resp.json())
        result.retry_count = int(resp.extensions.get("llm_retry_count", 0) or 0)
        return result

    async def arerank(
        self,
        query: str,
        candidates: list[dict],
        role: str,
        top_n: int = 3,
        *,
        soft_preferences: dict | None = None,
        ranking_weights: dict[str, float] | None = None,
        call_policy: LLMCallPolicy,
    ) -> RerankResult:
        """真异步重排：``LLMDeadlineExceeded`` 直接上抛，由 shadow 侧记 timeout。"""
        payload = build_rerank_payload(
            query=query,
            candidates=candidates,
            role=role,
            top_n=top_n,
            soft_preferences=soft_preferences,
            ranking_weights=ranking_weights,
        )

        try:
            resp = await call_llm_api_async(
                url=_chat_url(),
                headers=_build_headers(),
                payload=payload,
                call_policy=call_policy,
            )
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            error = LLMTimeout()
            error.llm_retry_count = int(getattr(exc, "llm_retry_count", 0) or 0)
            raise error from exc
        except httpx.HTTPStatusError as exc:
            raise LLMError(f"Doubao API HTTP error: {exc.response.status_code}")

        result = finalize_rerank_response(resp.json())
        result.retry_count = int(resp.extensions.get("llm_retry_count", 0) or 0)
        return result
