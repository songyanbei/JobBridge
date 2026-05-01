"""LLM provider 内部公用 helper。

仅供 provider 实现使用，业务层不应直接 import 本模块。
"""
import json
import logging

import httpx

from app.core.exceptions import LLMParseError
from app.llm.base import DialogueParseResult, IntentResult, RerankResult

logger = logging.getLogger(__name__)

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


def call_llm_api(
    *,
    url: str,
    headers: dict,
    payload: dict,
    timeout: int,
) -> httpx.Response:
    """调用 LLM REST API，最多重试 1 次。

    超时或网络错误时自动重试一次，两次都失败则抛出 httpx 异常。
    """
    for attempt in range(2):
        try:
            resp = httpx.post(
                url,
                headers=headers,
                json=payload,
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            if attempt == 0:
                logger.warning("LLM API attempt %d failed: %s, retrying...", attempt + 1, exc)
                continue
            raise
        except httpx.HTTPStatusError:
            raise


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
