"""LLM provider 内部公用 helper。

仅供 provider 实现使用，业务层不应直接 import 本模块。
"""
import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass

import httpx

from app.config import settings
from app.core.exceptions import LLMCircuitOpen, LLMParseError
from app.llm.base import (
    DialogueParseResult,
    IntentResult,
    LLMCallPolicy,
    LLMDeadlineExceeded,
    RerankResult,
)
from app.llm.prompts import (
    RERANK_SYSTEM_PROMPT,
    RERANK_USER_TEMPLATE,
    RERANK_USER_TEMPLATE_WITH_SOFT_PREF,
    format_soft_preferences_block,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 进程内熔断器
#
# 归属说明（review P2-30）：本段不是推荐改造引入的，来自
# codex/conversation-production-hardening（commit 06c2bca，已并入 origin/main），
# 目的是让 LLM 连续失败时快速拒绝，避免超时/重试风暴拖垮 worker 队列。
# 推荐改造不动它的阈值和语义，只在下面给"带 deadline 的 shadow 调用"分配了
# **独立的 circuit key 命名空间**：shadow 的超时不得把 legacy 的熔断器打开。
# ---------------------------------------------------------------------------


@dataclass
class _CircuitState:
    failures: int = 0
    open_until: float = 0.0
    probe_in_flight: bool = False


_circuit_lock = threading.Lock()
_circuits: dict[str, _CircuitState] = {}


def _reset_llm_circuits() -> None:
    """测试/运维热重载辅助：清除本进程 circuit 状态。"""
    with _circuit_lock:
        _circuits.clear()


def _before_llm_call(key: str) -> None:
    now = time.monotonic()
    with _circuit_lock:
        state = _circuits.setdefault(key, _CircuitState())
        if state.open_until <= 0:
            return
        if now < state.open_until or state.probe_in_flight:
            raise LLMCircuitOpen()
        # 恢复窗口后只允许一个 half-open 探针，防止并发洪峰同时穿透。
        state.probe_in_flight = True


def _record_llm_success(key: str) -> None:
    with _circuit_lock:
        _circuits[key] = _CircuitState()


def _record_llm_failure(key: str) -> None:
    threshold = max(1, int(settings.llm_circuit_failure_threshold))
    recovery = max(1, int(settings.llm_circuit_recovery_seconds))
    with _circuit_lock:
        state = _circuits.setdefault(key, _CircuitState())
        state.failures += 1
        state.probe_in_flight = False
        if state.failures >= threshold:
            state.open_until = time.monotonic() + recovery
            logger.error(
                "LLM circuit opened after %d failures; recovery_seconds=%d",
                state.failures, recovery,
            )

# 合法 intent 值白名单
VALID_INTENTS = frozenset({
    "upload_job", "upload_resume", "search_job", "search_worker",
    "upload_and_search", "follow_up", "show_more", "command", "chitchat",
})

# 阶段二（dialogue-intent-extraction-phased-plan §2.1.1）：DialogueParseResult 闭集。
VALID_DIALOGUE_ACTS = frozenset({
    "start_search", "modify_search", "answer_missing_slot",
    "show_more", "start_upload", "cancel", "reset",
    "resolve_conflict", "chitchat",
})
VALID_FRAME_HINTS = frozenset({
    "job_search", "candidate_search", "job_upload", "resume_upload", "none",
})
VALID_MERGE_HINT_VALUES = frozenset({"replace", "add", "remove", "unknown"})
VALID_CONFLICT_ACTIONS = frozenset({
    "cancel_draft", "resume_pending_upload", "proceed_with_new",
})


# ---------------------------------------------------------------------------
# 调用级 policy 解析（方案 §11.5）
# ---------------------------------------------------------------------------

def _resolve_policy(call_policy: LLMCallPolicy | None) -> LLMCallPolicy:
    """``None`` → legacy 默认（无 deadline、单次 30 秒、最多重试一次）。"""
    return call_policy if call_policy is not None else LLMCallPolicy()


def _circuit_key(url: str, policy: LLMCallPolicy) -> str:
    """带 deadline 的调用（= shadow）走独立熔断命名空间。

    shadow 用的是 3 秒预算，超时率天然高于 legacy 的 30 秒；如果共用一个 key，
    shadow 的 deadline 失败会把 legacy 的熔断器打开，等于用旁路观测把主链路
    打挂——与 §7.5"容量告警时优先丢弃 shadow，而不是降低 legacy 可用性"相反。
    """
    return url if policy.deadline_monotonic is None else f"{url}#deadline"


def _request_timeout_seconds(
    policy: LLMCallPolicy, base_timeout: float | None,
) -> float:
    """本次请求允许的秒数：调用方上限与 deadline 剩余量取小。

    Raises:
        LLMDeadlineExceeded: 剩余预算非正（连发请求都来不及）。
    """
    budget = float(base_timeout) if base_timeout is not None else float(
        settings.llm_timeout_seconds
    )
    remaining = policy.remaining_seconds()
    if remaining is None:
        return budget
    if remaining <= 0:
        raise LLMDeadlineExceeded(
            f"LLM call deadline exhausted before request (remaining={remaining:.3f}s)"
        )
    return min(budget, remaining)


def _can_retry(policy: LLMCallPolicy, attempt: int, attempts: int) -> bool:
    """重试次数同时受 ``max_retries`` 和剩余 deadline 限制（§11.5）。"""
    if attempt + 1 >= attempts:
        return False
    remaining = policy.remaining_seconds()
    return remaining is None or remaining > 0


def call_llm_api(
    *,
    url: str,
    headers: dict,
    payload: dict,
    timeout: float | None = None,
    call_policy: LLMCallPolicy | None = None,
) -> httpx.Response:
    """同步调用 LLM REST API。

    ``call_policy=None`` 即 legacy 基线：单次 timeout 取 ``timeout`` 或
    ``settings.llm_timeout_seconds``，超时/网络错误自动重试一次，两次都失败抛
    httpx 异常。

    ``call_policy`` 只用于统一接口和**调用级重试次数**；即便带了
    ``deadline_monotonic``，httpx 的同步 timeout 也是分阶段计时的，本函数不承诺
    严格的总耗时上限（§11.5）。需要硬 deadline 请改用
    :func:`call_llm_api_async`。
    """
    policy = _resolve_policy(call_policy)
    attempts = max(0, int(policy.max_retries)) + 1
    circuit_key = _circuit_key(url, policy)
    _before_llm_call(circuit_key)
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request_timeout = _request_timeout_seconds(policy, timeout)
        except LLMDeadlineExceeded:
            _record_llm_failure(circuit_key)
            raise
        try:
            resp = httpx.post(
                url,
                headers=headers,
                json=payload,
                timeout=request_timeout,
            )
            resp.raise_for_status()
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            last_error = exc
        except httpx.HTTPStatusError:
            _record_llm_failure(circuit_key)
            raise
        else:
            resp.extensions["llm_retry_count"] = attempt
            _record_llm_success(circuit_key)
            return resp

        if _can_retry(policy, attempt, attempts):
            logger.warning(
                "LLM API attempt %d failed: %s, retrying...", attempt + 1, last_error,
            )
            continue
        _record_llm_failure(circuit_key)
        setattr(last_error, "llm_retry_count", attempt)
        raise last_error


# ---------------------------------------------------------------------------
# 共享 AsyncClient（方案 §11.5 / §7.5）
#
# 生命周期由 shadow runner 统一管理：进程启动（或首次调用）时 get_async_client()，
# 应用 shutdown 时先停止接收新任务、等在途任务按各自 deadline 收敛，再
# await aclose_async_client()。禁止每次调用新建 client 或新建 event loop——
# 那会让"全局并发 4"退化成"每次调用一个连接池"，并把建连时间算进每一次预算。
# ---------------------------------------------------------------------------

_async_client: httpx.AsyncClient | None = None
_async_client_lock = threading.Lock()


def get_async_client() -> httpx.AsyncClient:
    """返回进程内共享的 ``httpx.AsyncClient``（幂等，必要时重建）。"""
    global _async_client
    client = _async_client
    if client is not None and not client.is_closed:
        return client
    with _async_client_lock:
        if _async_client is None or _async_client.is_closed:
            # 连接池按 shadow 全局并发上限留一倍余量：全局 permit 才是真正的
            # 并发闸门，连接池只负责不让单进程堆积过多空闲连接。
            concurrency = max(1, int(settings.recommendation_shadow_max_concurrency))
            _async_client = httpx.AsyncClient(
                timeout=httpx.Timeout(float(settings.llm_timeout_seconds)),
                limits=httpx.Limits(
                    max_connections=concurrency * 2,
                    max_keepalive_connections=concurrency,
                ),
            )
        return _async_client


async def aclose_async_client() -> None:
    """关闭共享 AsyncClient（应用 shutdown 调用；重复调用安全）。"""
    global _async_client
    with _async_client_lock:
        client, _async_client = _async_client, None
    if client is not None and not client.is_closed:
        await client.aclose()


async def call_llm_api_async(
    *,
    url: str,
    headers: dict,
    payload: dict,
    timeout: float | None = None,
    call_policy: LLMCallPolicy | None = None,
) -> httpx.Response:
    """异步调用 LLM REST API，兑现 ``call_policy`` 的绝对 deadline。

    每次请求（含准备重试时）都重新按 ``deadline_monotonic - monotonic()`` 计算
    remaining，并且：

    1. 构造 connect/read/write/pool **各阶段都 ≤ remaining** 的 ``httpx.Timeout``；
    2. 再用 ``asyncio.timeout(remaining)`` 包住完整的 ``await client.post(...)``。

    第 2 步是总时限的最终保障，不能省：httpx 的四个阶段各自计时，串起来可能远超
    remaining。remaining 非正时立即抛 :class:`LLMDeadlineExceeded`。
    """
    policy = _resolve_policy(call_policy)
    attempts = max(0, int(policy.max_retries)) + 1
    circuit_key = _circuit_key(url, policy)
    _before_llm_call(circuit_key)
    client = get_async_client()
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            budget = _request_timeout_seconds(policy, timeout)
        except LLMDeadlineExceeded:
            _record_llm_failure(circuit_key)
            raise
        try:
            async with asyncio.timeout(budget):
                resp = await client.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=httpx.Timeout(
                        budget, connect=budget, read=budget, write=budget, pool=budget,
                    ),
                )
            resp.raise_for_status()
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            last_error = exc
        except TimeoutError:
            # asyncio.timeout 触发：总预算用尽，与某个 socket 阶段超时区分开，
            # 让 shadow 侧能把它记成 shadow_timeout 而不是上游网络故障。
            last_error = LLMDeadlineExceeded(
                f"LLM async call exceeded its {budget:.3f}s budget"
            )
        except httpx.HTTPStatusError:
            _record_llm_failure(circuit_key)
            raise
        else:
            resp.extensions["llm_retry_count"] = attempt
            _record_llm_success(circuit_key)
            return resp

        if _can_retry(policy, attempt, attempts):
            logger.warning(
                "LLM async API attempt %d failed: %s, retrying...",
                attempt + 1, last_error,
            )
            continue
        _record_llm_failure(circuit_key)
        setattr(last_error, "llm_retry_count", attempt)
        raise last_error


def _intent_fallback(raw: str) -> IntentResult:
    """统一 IntentExtractor fallback：chitchat + confidence=0.0。"""
    return IntentResult(
        intent="chitchat",
        confidence=0.0,
        raw_response=raw or "",
    )


def parse_intent_response(raw: str) -> IntentResult:
    """从 LLM 原始输出中解析 IntentResult。

    Phase 7：基础结构错误（非 JSON / 非 dict）抛 ``LLMParseError`` 让上层
    把 ``status`` 记作 ``parse_failed``；字段级偏差仍走软兜底，保证业务连续。
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("IntentExtractor: JSON decode failed (%s), raising LLMParseError", exc)
        raise LLMParseError("intent_json_decode_failed")

    if not isinstance(data, dict):
        logger.warning("IntentExtractor: top-level value is not a dict, raising LLMParseError")
        raise LLMParseError("intent_not_a_dict")

    intent = data.get("intent", "chitchat")
    if not isinstance(intent, str) or intent not in VALID_INTENTS:
        logger.warning("IntentExtractor: unknown intent '%s', falling back to chitchat", intent)
        intent = "chitchat"

    confidence = data.get("confidence", 0.0)
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 0.0

    # 防御性类型校正：LLM 可能返回正确 key 但错误类型
    structured_data = data.get("structured_data", {})
    if not isinstance(structured_data, dict):
        structured_data = {}

    criteria_patch = data.get("criteria_patch", [])
    if not isinstance(criteria_patch, list):
        criteria_patch = []

    missing_fields = data.get("missing_fields", [])
    if not isinstance(missing_fields, list):
        missing_fields = []

    try:
        return IntentResult(
            intent=intent,
            structured_data=structured_data,
            criteria_patch=criteria_patch,
            missing_fields=missing_fields,
            confidence=confidence,
            raw_response=raw,
        )
    except Exception as exc:
        logger.warning("IntentExtractor: failed to build IntentResult: %s, falling back", exc)
        return _intent_fallback(raw)


def parse_dialogue_response(raw: str) -> DialogueParseResult:
    """从 LLM 原始输出中解析 DialogueParseResult（阶段二）。

    解析失败抛 LLMParseError；字段级偏差走软兜底（unknown act → chitchat 等），
    与 parse_intent_response 保持一致，使 classify_dialogue 能可靠 fallback 到 legacy。
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("DialogueParse: JSON decode failed (%s)", exc)
        raise LLMParseError("dialogue_json_decode_failed")

    if not isinstance(data, dict):
        logger.warning("DialogueParse: top-level value is not a dict")
        raise LLMParseError("dialogue_not_a_dict")

    dialogue_act = data.get("dialogue_act", "chitchat")
    if not isinstance(dialogue_act, str) or dialogue_act not in VALID_DIALOGUE_ACTS:
        logger.warning("DialogueParse: unknown dialogue_act '%s', falling back to chitchat",
                       dialogue_act)
        dialogue_act = "chitchat"

    frame_hint = data.get("frame_hint", "none")
    if not isinstance(frame_hint, str) or frame_hint not in VALID_FRAME_HINTS:
        frame_hint = "none"

    slots_delta = data.get("slots_delta", {})
    if not isinstance(slots_delta, dict):
        slots_delta = {}
    else:
        # LLM 偶尔会把字段值塞成 {"$gt":...}/{"op":"..."} 等结构；这些非法 shape 让下游
        # _normalize_structured_data 对未识别字段直接 passthrough（写入 search_criteria
        # 后被 SQLAlchemy 用作字面值），存在 SQL 失败风险。
        # 这里只允许：scalar / list / None；其它一律 drop。
        _SCALAR = (str, int, float, bool, type(None))
        slots_delta = {
            k: v
            for k, v in slots_delta.items()
            if isinstance(k, str) and (
                isinstance(v, _SCALAR) or isinstance(v, list)
            )
        }

    raw_merge = data.get("merge_hint", {}) or {}
    merge_hint: dict = {}
    if isinstance(raw_merge, dict):
        for k, v in raw_merge.items():
            if isinstance(k, str) and isinstance(v, str) and v in VALID_MERGE_HINT_VALUES:
                merge_hint[k] = v

    needs_clar = data.get("needs_clarification", False)
    needs_clar = bool(needs_clar) if isinstance(needs_clar, (bool, int)) else False

    confidence = data.get("confidence", 0.0)
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 0.0

    conflict_action = data.get("conflict_action")
    if conflict_action is not None:
        if not isinstance(conflict_action, str) or conflict_action not in VALID_CONFLICT_ACTIONS:
            conflict_action = None
    # 仅在 resolve_conflict 时保留 conflict_action，其它情况强制清空
    if dialogue_act != "resolve_conflict":
        conflict_action = None

    try:
        return DialogueParseResult(
            dialogue_act=dialogue_act,
            frame_hint=frame_hint,
            slots_delta=slots_delta,
            merge_hint=merge_hint,
            needs_clarification=needs_clar,
            confidence=confidence,
            conflict_action=conflict_action,
            raw_response=raw,
        )
    except Exception as exc:
        logger.warning("DialogueParse: failed to build DialogueParseResult: %s", exc)
        raise LLMParseError(f"dialogue_build_failed: {exc}")


def _rerank_fallback(raw: str) -> RerankResult:
    """统一 Reranker fallback：空结果。"""
    return RerankResult(
        ranked_items=[],
        reply_text="",
        raw_response=raw or "",
    )


def parse_rerank_response(raw: str) -> RerankResult:
    """从 LLM 原始输出中解析 RerankResult。

    Phase 7：基础结构错误（非 JSON / 非 dict）抛 ``LLMParseError``；
    字段级偏差仍走软兜底。
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("Reranker: JSON decode failed (%s), raising LLMParseError", exc)
        raise LLMParseError("rerank_json_decode_failed")

    if not isinstance(data, dict):
        logger.warning("Reranker: top-level value is not a dict, raising LLMParseError")
        raise LLMParseError("rerank_not_a_dict")

    # 防御性类型校正
    ranked_items = data.get("ranked_items", [])
    if not isinstance(ranked_items, list):
        ranked_items = []

    reply_text = data.get("reply_text", "")
    if not isinstance(reply_text, str):
        reply_text = str(reply_text) if reply_text is not None else ""

    try:
        return RerankResult(
            ranked_items=ranked_items,
            reply_text=reply_text,
            raw_response=raw,
        )
    except Exception as exc:
        logger.warning("Reranker: failed to build RerankResult: %s, falling back", exc)
        return _rerank_fallback(raw)


# ---------------------------------------------------------------------------
# OpenAI 兼容响应的公共解析（qwen / doubao 共用，避免规则漂移）
# ---------------------------------------------------------------------------

def extract_chat_content(resp_json: dict) -> str:
    """从 OpenAI 兼容响应中提取 assistant content。"""
    try:
        return resp_json["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""


def extract_chat_usage(resp_json: dict) -> tuple[int | None, int | None]:
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


def build_rerank_payload(
    *,
    query: str,
    candidates: list[dict],
    role: str,
    top_n: int,
    soft_preferences: dict | None = None,
    ranking_weights: dict[str, float] | None = None,
) -> dict:
    """构造 rerank 的 chat/completions payload。

    方案 §11.5：同步 ``rerank()`` 与异步 ``arerank()``、以及 qwen / 豆包两个
    provider **必须共享这一份 payload 构造**，否则 legacy 与 shadow 会在
    prompt 选择（v2.0 / v2.1 软偏好块）上悄悄分叉，双算差异不再可归因。
    """
    system_prompt = RERANK_SYSTEM_PROMPT.format(role=role, top_n=top_n)
    # Phase 5 §5.3：soft_preferences 非空时走 v2.1 prompt（带软偏好块）；
    # 为空时严格走 v2.0 等价路径（向后兼容验收）。
    if soft_preferences:
        user_prompt = RERANK_USER_TEMPLATE_WITH_SOFT_PREF.format(
            query=query,
            candidates=format_candidates(candidates),
            soft_preferences_block=format_soft_preferences_block(
                soft_preferences, ranking_weights,
            ),
        )
    else:
        user_prompt = RERANK_USER_TEMPLATE.format(
            query=query,
            candidates=format_candidates(candidates),
        )
    return {
        "model": settings.llm_reranker_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        # 探索由后端 §6.10 控制，不靠提高 LLM 温度（§11.5）。
        "temperature": 0.1,
    }


def finalize_rerank_response(resp_json: dict) -> RerankResult:
    """把 chat/completions 响应体解析为 RerankResult 并回填 token 用量。

    usage 先提再 parse：parse 失败时把 token 挂到 ``LLMParseError`` 上，让上层
    ``log_event`` 仍能记录真实用量（Phase 7 契约）。
    """
    raw = extract_chat_content(resp_json)
    in_tok, out_tok = extract_chat_usage(resp_json)
    try:
        result = parse_rerank_response(raw)
    except LLMParseError as exc:
        exc.input_tokens = in_tok
        exc.output_tokens = out_tok
        raise
    result.input_tokens = in_tok
    result.output_tokens = out_tok
    return result


def format_history(history: list[dict] | None) -> str:
    """将对话历史格式化为 prompt 中的文本。"""
    if not history:
        return "无"
    lines = []
    for turn in history:
        role = turn.get("role", "unknown")
        content = turn.get("content", "")
        lines.append(f"[{role}]: {content}")
    return "\n".join(lines)


def format_criteria(current_criteria: dict | None) -> str:
    """将当前累积检索条件格式化为 prompt 中的文本。"""
    if not current_criteria:
        return "无"
    return json.dumps(current_criteria, ensure_ascii=False, indent=2)


# Phase 1：session_hint 中真正对 LLM 有意义的键。其它字段（如 raw timestamp）
# 不进 prompt，避免把 prompt 拉长又抢占 token 预算。
_SESSION_HINT_KEYS_FOR_PROMPT = (
    "active_flow",
    "awaiting_fields",
    "awaiting_frame",
    "awaiting_field",
    "pending_upload_intent",
    "search_criteria",
    "broker_direction",
)


def format_session_hint(session_hint: dict | None) -> str:
    """把 session_hint 渲染为 prompt 用的结构化键值文本。

    Phase 1（dialogue-intent-extraction-phased-plan §1.1）：保持 JSON 结构而非
    长篇自然语言，避免拼装出歧义文本干扰 LLM 抽取。空字段直接返回"无"。
    """
    if not session_hint:
        return "无"
    compact: dict = {}
    for key in _SESSION_HINT_KEYS_FOR_PROMPT:
        if key not in session_hint:
            continue
        value = session_hint[key]
        # 空 list / 空 dict / None / 空串：跳过，减少噪声
        if value is None:
            continue
        if isinstance(value, (list, dict, str)) and not value:
            continue
        compact[key] = value
    if not compact:
        return "无"
    return json.dumps(compact, ensure_ascii=False, indent=2)


def format_candidates(candidates: list[dict]) -> str:
    """将候选列表格式化为 prompt 中的文本。"""
    if not candidates:
        return "无"
    lines = []
    for i, c in enumerate(candidates, 1):
        lines.append(f"{i}. {json.dumps(c, ensure_ascii=False)}")
    return "\n".join(lines)
