"""阶段二 Dialogue Reducer（dialogue-intent-extraction-phased-plan §2.1）。

reduce(parse_result, session, role) 是**纯函数**：
- 输入只读：DialogueParseResult / SessionState / role
- 输出：DialogueDecision，不写 session、不调 LLM、不调 handler
- 所有 session 写入意图通过 state_transition / awaiting_ops / pending_interruption
  这三个声明式字段表达，由 dialogue_applier.apply_decision 物化。

设计要点：
1. **active_flow 是 source of truth**（current-state §3.1）。frame_hint 与 active_flow
   冲突时优先按后端状态裁决，不让 LLM 直接覆盖。
2. **resolved_merge_policy 由后端决定**：LLM 给的 merge_hint 仅作为弱信号；
   裸值 / 模糊表达统一按 ambiguous_city_query_policy 配置裁决。
3. **schema 校验 / missing 重算复用阶段一 helper**：_legacy_required /
   _legacy_valid_fields / _legacy_compute_missing。阶段三换 slot_schema 时
   只换 helper 内部实现，调用方不动。
4. **置信度兜底**：confidence < settings.low_confidence_threshold 且本轮触及关键字段，
   或 frame 冲突无法消解时，强制 needs_clarification=true。
5. **post_search_action 固定 'none'**：Phase 5 结果感知策略的兼容预留位，
   阶段二到阶段四不参与路由（phased-plan §2.1.2）。
"""
from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from app.config import settings
from app.dialogue import slot_schema
from app.llm.base import DialogueParseResult
from app.schemas.conversation import SessionState
from app.services import conversation_service
from app.services.intent_service import (
    _legacy_compute_missing,
    _legacy_required,
    _legacy_valid_fields,
    _normalize_int_field,
    _normalize_structured_data,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 阶段三：低置信度兜底关心的关键字段集合从 slot_schema 派生（hard + askable
# 的 search frame 字段），避免硬编码与 schema drift。
def _key_fields_for_low_confidence() -> frozenset[str]:
    return slot_schema.key_fields_for_low_confidence()


# 角色权限映射：阶段三委托 slot_schema.check_role_permission，本文件不再保留常量。


# ---------------------------------------------------------------------------
# DTO
# ---------------------------------------------------------------------------


class DialogueDecision(BaseModel):
    """后端裁决层 DTO（dialogue-intent-extraction-phased-plan §2.1.1）。"""

    dialogue_act: str
    resolved_frame: Literal[
        "job_search", "candidate_search",
        "job_upload", "resume_upload", "none",
    ]
    accepted_slots_delta: dict = Field(default_factory=dict)
    # 仅对 accepted_slots_delta 中存在的 key 有意义；其它字段为隐式 keep。
    resolved_merge_policy: dict[str, Literal["replace", "add", "remove"]] = Field(
        default_factory=dict,
    )
    final_search_criteria: dict = Field(default_factory=dict)
    missing_slots: list[str] = Field(default_factory=list)
    route_intent: str  # 兼容派生层使用（dialogue_compat）
    clarification: dict | None = None  # {kind, ambiguous_field?, options?}
    state_transition: Literal[
        "none",
        "enter_upload_conflict",
        "exit_upload_conflict",
        "enter_search_active",
        "reset_search",
        "clear_awaiting",
        "clear_pending_upload",
        "resume_upload_collecting",
        "apply_pending_interruption",
    ] = "none"
    pending_interruption: dict | None = None
    awaiting_ops: list[dict] = Field(default_factory=list)
    # Phase 5 兼容预留位，阶段二到阶段四固定 'none'，不参与路由
    post_search_action: Literal["none"] = "none"


# ---------------------------------------------------------------------------
# 内部 helper
# ---------------------------------------------------------------------------


def _has_search_context(session: SessionState) -> bool:
    return bool(session.search_criteria)


def _is_role_allowed(role: str, frame: str) -> bool:
    """角色权限校验（阶段三委托 slot_schema.check_role_permission）。"""
    return slot_schema.check_role_permission(role, frame)


def _validate_and_normalize_slots(
    frame: str, slots_delta: dict, role: str,
) -> tuple[dict, list[str]]:
    """按 frame 合法字段集过滤 + 用 intent_service 归一化函数清洗。

    阶段三流程：
    1. slot_schema.remap_synonyms 把 expected_* 等同义字段先归并到 canonical key
       （吸收 intent_service._SEARCH_FIELD_REMAP 的兼容兜底语义）；
    2. slot_schema.validate_slots_delta 按 fields_for(frame) 做合法字段过滤；
    3. _normalize_structured_data 走既有归一化函数（city/job_category/int range）。

    返回 (accepted, dropped_field_names)。
    """
    if not slots_delta:
        return {}, []
    remapped = slot_schema.remap_synonyms(frame, slots_delta)
    accepted_raw, dropped = slot_schema.validate_slots_delta(frame, remapped)
    # 用 _normalize_structured_data 复用归一化（city / job_category / int range）。
    # intent 仅决定 force_list 行为：搜索用 list，上传用标量。
    pseudo_intent = _frame_to_intent(frame)
    normalized = _normalize_structured_data(
        accepted_raw, role=role, intent=pseudo_intent,
    )
    return normalized, dropped


def _frame_to_intent(frame: str) -> str:
    """frame 名映射成 intent_service.normalize 用得到的 intent 字面量。"""
    return {
        "job_search": "search_job",
        "candidate_search": "search_worker",
        "job_upload": "upload_job",
        "resume_upload": "upload_resume",
    }.get(frame, "chitchat")


def _resolve_merge_policy(
    frame: str,
    field: str,
    new_value,
    old_value,
    merge_hint: dict,
) -> tuple[Literal["replace", "add", "remove"], dict | None]:
    """对单字段决策最终 merge_policy。

    阶段三：默认策略由 ``slot_schema.default_merge_policy(frame, field, has_old)``
    提供，reducer 只在 schema 返回 ``clarify`` 时叠加业务规则（city 字段叠
    ``ambiguous_city_query_policy``）。LLM 明确 hint 优先级最高。

    返回 (policy, clarification_or_none)。clarification 不为 None 表示需要反问。
    """
    hint = merge_hint.get(field)
    has_old = bool(old_value)

    # 1) LLM 明确给出 → 按 LLM
    if hint in ("replace", "add", "remove"):
        return hint, None

    # 2) 没旧值 → 直接 replace（写入即可）
    if not has_old:
        return "replace", None

    # 3) 有旧值 + hint=unknown / 缺失 → 走 schema 声明的 default_merge
    schema_policy = slot_schema.default_merge_policy(frame, field, has_old)
    if schema_policy in ("replace", "add"):
        return schema_policy, None

    # schema_policy == "clarify"：默认要反问，按字段叠加业务策略
    if field == "city":
        # 「北京有吗 + 已有西安」歧义：受 settings.ambiguous_city_query_policy 控制
        cfg = getattr(settings, "ambiguous_city_query_policy", "clarify")
        if cfg == "replace":
            return "replace", None
        return "replace", {
            "kind": "city_replace_or_add",
            "ambiguous_field": "city",
            "options": ["replace", "add"],
            "old_value": list(old_value) if isinstance(old_value, list) else [old_value],
            "new_value": list(new_value) if isinstance(new_value, list) else [new_value],
        }

    # 其它声明 clarify 但还没业务策略的字段（schema 后续可能扩展）：保守 replace
    return "replace", None


def _merge_value(
    field: str, policy: str, new_value, old_value,
):
    """根据 policy 合并字段值。返回 final_value。"""
    if policy == "replace":
        return new_value
    if policy == "add":
        # list 字段取并集；非 list 退化为 replace
        if isinstance(new_value, list) and isinstance(old_value, list):
            seen: set = set()
            out: list = []
            for v in (old_value or []) + (new_value or []):
                key = v
                if key in seen:
                    continue
                seen.add(key)
                out.append(v)
            return out
        return new_value
    if policy == "remove":
        if isinstance(old_value, list) and isinstance(new_value, list):
            removeset = set(new_value or [])
            return [v for v in (old_value or []) if v not in removeset]
        return old_value
    return new_value


def _try_match_bare_value(text: str, awaiting_fields: list[str]) -> dict:
    """对裸数值按字段类型 + 取值范围 tie-break。

    复用阶段一 _normalize_int_field 的 lo/hi 行为，避免重复。
    awaiting_fields 中只有薪资类字段时，裸值优先落薪资。
    """
    if not text or not awaiting_fields:
        return {}
    stripped = text.strip()
    if not stripped:
        return {}
    # 仅消费纯数字
    try:
        int(stripped)
    except (TypeError, ValueError):
        return {}
    for field in awaiting_fields:
        if field in {"salary_floor_monthly", "salary_ceiling_monthly"}:
            v = _normalize_int_field(stripped, lo=500, hi=200_000)
            if v is not None:
                return {field: v}
    return {}


def _frame_for_active_flow(active_flow: str | None) -> str | None:
    """active_flow → 隐含 frame（用于冲突判定）。"""
    return {
        "upload_collecting": "_upload",
        "search_active": "_search",
    }.get(active_flow or "")


def _is_upload_to_search_conflict(active_flow: str | None, frame_hint: str) -> bool:
    return active_flow == "upload_collecting" and frame_hint in (
        "job_search", "candidate_search",
    )


def _build_pending_interruption(
    parse_result: DialogueParseResult, frame: str, slots: dict,
) -> dict:
    """从 parse_result + frame + accepted slots 派生 pending_interruption。

    与阶段一 _enter_upload_conflict 保留的字段（intent/structured_data/criteria_patch/raw_text）
    保持兼容，便于 applier 直接调 _enter_upload_conflict。
    """
    return {
        "intent": _frame_to_intent(frame),
        "structured_data": dict(slots or {}),
        "criteria_patch": [],
        "raw_text": parse_result.raw_response or "",
    }


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def reduce(
    parse_result: DialogueParseResult,
    session: SessionState,
    role: str,
    *,
    raw_text: str = "",
) -> DialogueDecision:
    """把 LLM parse 结果裁决成后端 decision。

    raw_text 仅用于 awaiting tie-break 裸值（_try_match_bare_value），
    不用于改变 dialogue_act / frame_hint 的语义。
    """
    act = parse_result.dialogue_act
    frame_hint = parse_result.frame_hint

    # 0) cancel / reset / chitchat / show_more 短路
    if act == "cancel":
        # 当前没有可取消流程时不改 session（applier 处会根据 active_flow 决定文案）
        return DialogueDecision(
            dialogue_act=act, resolved_frame="none",
            route_intent="command",
            accepted_slots_delta={},
            final_search_criteria=dict(session.search_criteria or {}),
            state_transition=(
                "clear_pending_upload"
                if session.active_flow == "upload_collecting"
                else "clear_awaiting"
                if session.awaiting_fields
                else "none"
            ),
        )
    if act == "reset":
        return DialogueDecision(
            dialogue_act=act, resolved_frame="none",
            route_intent="command",
            accepted_slots_delta={},
            final_search_criteria={},
            state_transition="reset_search",
        )
    if act == "chitchat":
        return DialogueDecision(
            dialogue_act=act, resolved_frame="none",
            route_intent="chitchat",
            final_search_criteria=dict(session.search_criteria or {}),
        )
    if act == "show_more":
        return DialogueDecision(
            dialogue_act=act, resolved_frame="none",
            route_intent="show_more",
            final_search_criteria=dict(session.search_criteria or {}),
        )

    # 1) resolve_conflict 路径（仅 active_flow=upload_conflict 上下文有意义）
    if act == "resolve_conflict":
        return _reduce_resolve_conflict(parse_result, session)

    # 2) frame_hint vs active_flow 冲突：upload_collecting → search → enter_upload_conflict
    if _is_upload_to_search_conflict(session.active_flow, frame_hint):
        # 先把本轮 LLM 抽到的字段过一遍 schema/normalize，作为 pending_interruption 携带
        accepted, _dropped = _validate_and_normalize_slots(
            frame_hint, parse_result.slots_delta, role,
        )
        pending = _build_pending_interruption(parse_result, frame_hint, accepted)
        return DialogueDecision(
            dialogue_act=act,
            resolved_frame=frame_hint,
            accepted_slots_delta=accepted,
            final_search_criteria=dict(session.search_criteria or {}),
            route_intent=_frame_to_intent(frame_hint),
            state_transition="enter_upload_conflict",
            pending_interruption=pending,
        )

    # 3) role 权限拒绝（worker → job_upload 等）
    if frame_hint != "none" and not _is_role_allowed(role, frame_hint):
        return DialogueDecision(
            dialogue_act=act, resolved_frame=frame_hint,
            route_intent="chitchat",
            final_search_criteria=dict(session.search_criteria or {}),
            clarification={
                "kind": "role_no_permission",
                "ambiguous_field": None,
                "options": [],
            },
            accepted_slots_delta={},
        )

    # 4) start_upload / answer_missing_slot / start_search / modify_search 主路径
    return _reduce_main(parse_result, session, role, raw_text=raw_text)


def _reduce_resolve_conflict(
    parse_result: DialogueParseResult, session: SessionState,
) -> DialogueDecision:
    """处理 resolve_conflict + conflict_action（phased-plan §2.1.8）。"""
    action = parse_result.conflict_action
    transition: str = "none"
    if action == "cancel_draft":
        transition = "clear_pending_upload"
    elif action == "resume_pending_upload":
        transition = "resume_upload_collecting"
    elif action == "proceed_with_new":
        transition = "apply_pending_interruption"
    return DialogueDecision(
        dialogue_act="resolve_conflict",
        resolved_frame="none",
        accepted_slots_delta={},
        final_search_criteria=dict(session.search_criteria or {}),
        route_intent="command",
        state_transition=transition,  # type: ignore[arg-type]
    )


def _reduce_main(
    parse_result: DialogueParseResult,
    session: SessionState,
    role: str,
    *,
    raw_text: str,
) -> DialogueDecision:
    """主路径：start_search / modify_search / answer_missing_slot / start_upload。"""
    act = parse_result.dialogue_act
    frame_hint = parse_result.frame_hint or "none"

    # 决定本轮 resolved_frame：上传 act 用 frame_hint；搜索 act 缺 frame_hint 时按已有
    # search_criteria 推（避免「裸数值补槽」frame_hint=none 的情况丢 frame）
    resolved_frame = _resolve_frame(act, frame_hint, session, role)

    # 搜索类 act 但 frame 解析不到 → 反问而不是静默 0 命中（adversarial review C5）。
    # 触发条件：start_search/modify_search/answer_missing_slot 且 resolved_frame=none。
    if (
        act in {"start_search", "modify_search", "answer_missing_slot"}
        and resolved_frame == "none"
    ):
        return DialogueDecision(
            dialogue_act=act,
            resolved_frame="none",
            accepted_slots_delta={},
            final_search_criteria=dict(session.search_criteria or {}),
            route_intent="chitchat",
            clarification={
                "kind": "low_confidence",
                "ambiguous_field": None,
                "options": [],
            },
        )

    # 4.1 awaiting tie-break：answer_missing_slot 路径下，裸值落薪资字段
    extra_from_awaiting: dict = {}
    awaiting_active = (
        session.awaiting_fields
        and session.awaiting_frame == resolved_frame
        and not conversation_service.is_search_awaiting_expired(session)
    )
    if act == "answer_missing_slot" and not parse_result.slots_delta and awaiting_active:
        extra_from_awaiting = _try_match_bare_value(
            raw_text, list(session.awaiting_fields),
        )

    # 合并 LLM slots_delta 与 awaiting tie-break
    slots_input: dict = dict(parse_result.slots_delta or {})
    slots_input.update(extra_from_awaiting)

    # 4.2 schema 校验 + 归一化
    accepted, dropped = _validate_and_normalize_slots(
        resolved_frame, slots_input, role,
    )
    if dropped:
        logger.info(
            "dialogue_v2_dropped_slots: frame=%s dropped=%s",
            resolved_frame, sorted(dropped),
        )

    # 4.3 决定 resolved_merge_policy（仅对 accepted 中存在的 key）
    old_criteria = dict(session.search_criteria or {})
    resolved_policy: dict[str, str] = {}
    final_criteria = dict(old_criteria)
    pending_clarification: dict | None = None
    for field, new_value in accepted.items():
        old_value = old_criteria.get(field)
        policy, clar = _resolve_merge_policy(
            resolved_frame, field, new_value, old_value,
            parse_result.merge_hint or {},
        )
        if clar is not None and pending_clarification is None:
            pending_clarification = clar
            # clarify 路径：保留旧值不动；不写 final_criteria
            continue
        resolved_policy[field] = policy
        final_criteria[field] = _merge_value(field, policy, new_value, old_value)

    # 4.4 missing_slots 由后端 schema 重算
    missing_slots: list[str] = []
    if resolved_frame in {
        "job_search", "candidate_search", "job_upload", "resume_upload",
    }:
        missing_slots = _legacy_compute_missing(resolved_frame, final_criteria)

    # 4.5 置信度兜底：低 confidence + 触及关键字段 → 强制反问
    forced_low_conf = False
    if pending_clarification is None:
        touches_key = bool(_key_fields_for_low_confidence() & set(accepted.keys()))
        threshold = getattr(settings, "low_confidence_threshold", 0.6)
        if (parse_result.confidence < threshold) and touches_key:
            pending_clarification = {
                "kind": "low_confidence",
                "ambiguous_field": None,
                "options": [],
            }
            forced_low_conf = True

    # 4.6 needs_clarification 输出位 + 派生 route_intent
    needs_clar = pending_clarification is not None or parse_result.needs_clarification

    # 4.7 awaiting_ops（声明式，applier 物化）
    awaiting_ops: list[dict] = []
    if act == "answer_missing_slot" and accepted:
        awaiting_ops.append({
            "op": "consume",
            "fields": list(accepted.keys()),
        })

    # 4.8 派生 route_intent（仅做兼容映射，dialogue_compat 也会用同一份逻辑）
    route_intent = _derive_route_intent(
        act, resolved_frame, session, has_existing_criteria=bool(old_criteria),
    )

    # 4.9 state_transition：start_search 在 idle 时进入 search_active；
    # 这里仅描述意图，applier 才真正写 active_flow。
    state_transition: str = "none"
    if (
        act in {"start_search", "modify_search", "answer_missing_slot"}
        and resolved_frame in {"job_search", "candidate_search"}
        and not pending_clarification
    ):
        state_transition = "enter_search_active"

    return DialogueDecision(
        dialogue_act=act,
        resolved_frame=resolved_frame,
        accepted_slots_delta=accepted,
        resolved_merge_policy=resolved_policy,
        final_search_criteria=final_criteria,
        missing_slots=missing_slots,
        route_intent=route_intent,
        clarification=pending_clarification,
        state_transition=state_transition,  # type: ignore[arg-type]
        awaiting_ops=awaiting_ops,
    )


def _resolve_frame(
    act: str, frame_hint: str, session: SessionState, role: str,
) -> Literal["job_search", "candidate_search", "job_upload", "resume_upload", "none"]:
    """决定 resolved_frame。

    - frame_hint != none → 直接用（前提是已经过 role 权限校验）
    - frame_hint == none + 搜索类 act + 已有 search_criteria → 按 role 继承
    - 否则 → none
    """
    if frame_hint in {"job_search", "candidate_search", "job_upload", "resume_upload"}:
        return frame_hint  # type: ignore[return-value]
    if act in {"modify_search", "answer_missing_slot"} and session.search_criteria:
        # 推断当前 search frame：role + broker_direction
        if role == "worker":
            return "job_search"
        if role == "factory":
            return "candidate_search"
        if role == "broker":
            direction = getattr(session, "broker_direction", None)
            if direction == "search_worker":
                return "candidate_search"
            return "job_search"
    return "none"


def _derive_route_intent(
    act: str,
    frame: str,
    session: SessionState,
    *,
    has_existing_criteria: bool,
) -> str:
    """与 dialogue_compat.decision_to_intent_result 的 route_intent 派生保持一致。

    抽出来是因为 reducer 自身也要写 route_intent 字段；compat 层会读这个值。
    """
    if act == "start_upload":
        if frame == "job_upload":
            return "upload_job"
        if frame == "resume_upload":
            return "upload_resume"
        return "chitchat"
    if act == "start_search":
        if has_existing_criteria:
            # 已有 criteria 的 start_search 视为 follow_up（避免清旧条件）
            return "follow_up"
        if frame == "candidate_search":
            return "search_worker"
        return "search_job"
    if act in {"modify_search", "answer_missing_slot"}:
        return "follow_up"
    if act == "show_more":
        return "show_more"
    if act in {"cancel", "reset"}:
        return "command"
    if act == "resolve_conflict":
        return "command"
    return "chitchat"
