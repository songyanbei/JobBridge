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
import re
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

# broker 同时能找岗位和找工人，模型偶尔会把“工人”这个词本身误当成搜索对象。
# 这里只覆盖语义关系足够明确的句式：谁是岗位的受益人，或谁是招聘方。模糊表达
# （例如“看看苏州电工”）仍交给 LLM + session.broker_direction，不做关键词猜测。
_BROKER_JOB_BENEFICIARY_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        (
            r"(?:^|[，,。；;！？!?\s])"
            r"(?:我(?:这边|这里|手上)?|这边|手上)?"
            r"(?:有|来了?|带着?)"
            r"(?:一|两|几)?(?:个|位|名)?"
            r"(?:工人|师傅|求职者|打工者)"
            r".{0,24}?(?:想|要|希望|打算|准备)(?:去|到|在|找|换|做)"
        ),
        (
            r"(?:帮|替|给)"
            r"(?:这|那|一|一个|这位|那位|这名|那名)?(?:个|位|名)?"
            r"(?:工人|师傅|求职者|打工者)"
            r".{0,12}?(?:找|看看|推荐)"
            r".{0,12}?(?:工作|岗位|职位|活)"
        ),
        (
            r"(?:工人|师傅|求职者|打工者)"
            r".{0,8}?(?:想找|要找|希望找|想换|要换)"
            r".{0,8}?(?:工作|岗位|职位|活)"
        ),
    )
)

_BROKER_RECRUITER_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        (
            r"(?:帮|替|给).{0,8}?"
            r"(?:企业|工厂|厂家|公司|招聘方|老板)"
            r".{0,12}?(?:找|招|招聘|物色|推荐)"
            r".{0,16}?(?:工人|师傅|员工|人选|候选人)"
        ),
        (
            r"(?:企业|工厂|厂家|公司|招聘方|老板)"
            r".{0,10}?(?:要|想|需要|缺|招聘|招|找)"
            r".{0,16}?(?:工人|师傅|员工|人选|候选人)"
        ),
        (
            r"(?:找|招|招聘|物色)"
            r"(?:一|两|几|\d+)?(?:个|位|名)?"
            r"[^，,。；;！？!?]{0,16}?(?:工人|师傅|员工|人选|候选人)"
        ),
    )
)


def broker_explicit_direction(raw_text: str) -> str | None:
    """Return the direction encoded by an unambiguous broker subject/object phrase."""
    if not raw_text:
        return None
    job_hit = any(
        pattern.search(raw_text) for pattern in _BROKER_JOB_BENEFICIARY_PATTERNS
    )
    worker_hit = any(
        pattern.search(raw_text) for pattern in _BROKER_RECRUITER_PATTERNS
    )
    if job_hit == worker_hit:
        return None
    return "search_job" if job_hit else "search_worker"

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
        # Phase 5 §5.2：放宽确认状态机
        # apply_relaxation / cancel_relaxation 不在 dialogue_applier 处理（拿不到 db），
        # 由 message_router._route_v2_relaxation_response 接管；apply_decision 仅显式
        # no-op 不告警，避免误调时干扰日志。
        "apply_relaxation",
        "cancel_relaxation",
        # clear_pending_relaxation 在 dialogue_applier 处理（仅 session 写入）
        "clear_pending_relaxation",
    ] = "none"
    pending_interruption: dict | None = None
    awaiting_ops: list[dict] = Field(default_factory=list)
    # Phase 5 §5.0：post_search_action Literal 集合从 ["none"] 扩到完整 7 个 action。
    # 但本子阶段 reducer 默认仍输出 'none'（后续由 post_search_reduce 替代输出）；
    # 5.0 message_router 不消费该字段，5.1 起才接通。
    post_search_action: Literal[
        "none",
        "no_action",
        "show_results",
        "show_results_with_soft_pref_notice",
        "auto_relax_and_retry",
        "suggest_relaxation",
        "ask_clarification",
        "paginate_no_more",
    ] = "none"


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

    # A repeated, normalized city is not an ambiguity. This commonly happens
    # when a user searches the same city again after a prior turn; asking
    # whether to replace or add would render the same city twice.
    if field == "city":
        old_values = old_value if isinstance(old_value, list) else [old_value]
        new_values = new_value if isinstance(new_value, list) else [new_value]
        if set(old_values) == set(new_values):
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


def _apply_broker_direction_anchor(
    parse_result: DialogueParseResult,
    role: str,
    raw_text: str,
) -> DialogueParseResult:
    """用明确的主客体句式约束 broker 搜索方向。

    该护栏只处理 search act，且仅在岗位受益人和招聘方两个锚点恰好命中一侧时
    生效；双侧都命中或都不命中时保留模型判断。方向改写时同步映射薪资字段，
    避免原 frame 的 ceiling/floor 字段在 schema 校验时被静默丢弃。
    """
    if (
        role != "broker"
        or parse_result.dialogue_act
        not in {"start_search", "modify_search", "answer_missing_slot"}
        or not raw_text.strip()
    ):
        return parse_result

    explicit_direction = broker_explicit_direction(raw_text)
    if explicit_direction is None:
        return parse_result

    target_frame = (
        "job_search"
        if explicit_direction == "search_job"
        else "candidate_search"
    )
    if parse_result.frame_hint == target_frame:
        return parse_result

    slots = dict(parse_result.slots_delta or {})
    if target_frame == "job_search" and "salary_ceiling_monthly" in slots:
        slots.setdefault("salary_floor_monthly", slots.pop("salary_ceiling_monthly"))
    elif target_frame == "candidate_search" and "salary_floor_monthly" in slots:
        slots.setdefault("salary_ceiling_monthly", slots.pop("salary_floor_monthly"))

    logger.info(
        "dialogue_v2_broker_direction_guard: frame=%s -> %s",
        parse_result.frame_hint,
        target_frame,
    )
    return parse_result.model_copy(
        update={"frame_hint": target_frame, "slots_delta": slots},
    )


def _apply_single_direction_role_guard(
    parse_result: DialogueParseResult,
    role: str,
    raw_text: str,
) -> DialogueParseResult:
    """把 worker/factory 的搜索 frame 约束到其唯一授权方向。

    这两个角色不存在 broker 式双向歧义：worker 只能找岗位，factory 只能找
    工人。对 search act 纠正模型漂移比返回无权限提示更符合用户字面动作，也避免
    同一句话因 provider 随机性偶发失败。
    """
    if not raw_text.strip() or parse_result.dialogue_act not in {
        "start_search", "modify_search", "answer_missing_slot",
    }:
        return parse_result
    target_frame = {
        "worker": "job_search",
        "factory": "candidate_search",
    }.get(role)
    if (
        target_frame is None
        or parse_result.frame_hint not in {"job_search", "candidate_search"}
        or parse_result.frame_hint == target_frame
    ):
        return parse_result

    # 明确请求了角色无权执行的反方向动作时，保留模型 frame，让后续权限层返回
    # role_no_permission；只纠正没有这种字面证据的 provider 漂移。
    explicit_wrong_direction = (
        role == "factory"
        and bool(re.search(
            r"(?:找|看看|推荐).{0,10}(?:工作|岗位|职位|活)"
            r"|(?:我|本人|工人|师傅).{0,10}(?:想|要|希望).{0,12}"
            r"(?:工作|岗位|职位|活)",
            raw_text,
        ))
    ) or (
        role == "worker"
        and bool(re.search(
            r"(?:找|招|招聘|需要|缺).{0,16}"
            r"(?:工人|师傅|候选人|人选|员工)"
            r"|(?:企业|工厂|厂家|公司|招聘方|老板).{0,16}"
            r"(?:找|招|招聘)",
            raw_text,
        ))
    )
    if explicit_wrong_direction:
        return parse_result

    slots = dict(parse_result.slots_delta or {})
    if target_frame == "job_search" and "salary_ceiling_monthly" in slots:
        slots.setdefault("salary_floor_monthly", slots.pop("salary_ceiling_monthly"))
    elif target_frame == "candidate_search" and "salary_floor_monthly" in slots:
        slots.setdefault("salary_ceiling_monthly", slots.pop("salary_floor_monthly"))
    logger.info(
        "dialogue_v2_role_direction_guard: role=%s frame=%s -> %s",
        role,
        parse_result.frame_hint,
        target_frame,
    )
    return parse_result.model_copy(
        update={"frame_hint": target_frame, "slots_delta": slots},
    )


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

    raw_text 用于 awaiting tie-break 裸值，以及 broker 明确主客体句式的方向护栏；
    后者不处理模糊表达，也不改变 dialogue_act。
    """
    parse_result = _apply_single_direction_role_guard(
        parse_result, role, raw_text,
    )
    parse_result = _apply_broker_direction_anchor(
        parse_result, role, raw_text,
    )
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

    # 1.bis) Phase 5 §5.2：respond_relaxation_offer 路径（与 resolve_conflict 平行
    # 但完全独立——upload_conflict 流程 vs 搜索放宽流程）
    if act == "respond_relaxation_offer":
        return _reduce_respond_relaxation_offer(parse_result, session)

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


def _reduce_respond_relaxation_offer(
    parse_result: DialogueParseResult, session: SessionState,
) -> DialogueDecision:
    """phased-plan §5.2.3 dialogue_reducer 行：把 respond_relaxation_offer +
    relaxation_response 翻译为 state_transition。

    防御 LLM 误判（pending_relaxation 为 None 时输出 respond_relaxation_offer）：
    降级为 chitchat（phased-plan §5.2.3 test_dialogue_reducer 防御单测）。
    """
    if not session.pending_relaxation:
        # 误判：reducer 不应让该 act 通过；降级为 chitchat 走通用路径。
        logger.info(
            "dialogue_v2_relaxation_no_context: respond_relaxation_offer "
            "without pending_relaxation; downgrading to chitchat"
        )
        return DialogueDecision(
            dialogue_act="chitchat",
            resolved_frame="none",
            accepted_slots_delta={},
            final_search_criteria=dict(session.search_criteria or {}),
            route_intent="chitchat",
        )

    response = parse_result.relaxation_response
    if response == "accept":
        transition = "apply_relaxation"
    elif response == "reject":
        transition = "cancel_relaxation"
    else:
        # 缺失或非法 → 降级为 chitchat（防御）
        transition = "none"

    return DialogueDecision(
        dialogue_act="respond_relaxation_offer",
        resolved_frame="none",
        accepted_slots_delta={},
        final_search_criteria=dict(session.search_criteria or {}),
        route_intent="follow_up",
        state_transition=transition,  # type: ignore[arg-type]
    )


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

    # Broker 的两种搜索面向不同实体，筛选条件不能跨方向继承。用户明确从
    # “找工人”切到“找岗位”（或反向）时，把本轮视为一次全新的搜索。
    broker_direction_switch = (
        role == "broker"
        and act in {"start_search", "modify_search", "answer_missing_slot"}
        and resolved_frame in {"job_search", "candidate_search"}
        and session.broker_direction in {"search_job", "search_worker"}
        and (
            (resolved_frame == "job_search" and session.broker_direction != "search_job")
            or (
                resolved_frame == "candidate_search"
                and session.broker_direction != "search_worker"
            )
        )
    )

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

    pending_clarification: dict | None = None

    # 4.2b（codex review P1-3 修复）：drop 后无有效字段 + 本轮需要业务动作 → clarify。
    # 与 phased-plan §5「失败模式」对齐：避免脏 parse 静默继续旧搜索条件。
    # 业务动作 act 集合：start_search / modify_search / answer_missing_slot / start_upload。
    # cancel / reset / show_more / chitchat / resolve_conflict 等非业务动作不触发。
    _BUSINESS_ACTS = {
        "start_search", "modify_search", "answer_missing_slot", "start_upload",
    }
    if dropped and not accepted and act in _BUSINESS_ACTS:
        pending_clarification = {
            "kind": "dropped_slots_no_valid",
            "ambiguous_field": None,
            "options": sorted(dropped),
        }

    # 4.3 决定 resolved_merge_policy（仅对 accepted 中存在的 key）
    old_criteria = (
        {}
        if broker_direction_switch or resolved_frame in {"job_upload", "resume_upload"}
        else dict(session.search_criteria or {})
    )
    resolved_policy: dict[str, str] = {}
    final_criteria = dict(old_criteria)
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

    # 4.5b（codex review P1-2 修复）：透传 LLM needs_clarification=True 信号。
    # 之前 needs_clar 变量算了但未使用，导致 LLM 显式请求澄清时被静默丢弃，
    # 系统继续按 follow_up 用旧条件执行（plan §3.3：reducer 可覆盖 LLM 的
    # needs_clarification，但反过来 LLM 已要求澄清时也必须生成 decision.clarification）。
    if pending_clarification is None and parse_result.needs_clarification:
        pending_clarification = {
            "kind": "llm_requested",
            "ambiguous_field": None,
            "options": [],
        }

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
    if broker_direction_switch:
        # The provider may call an explicit new-object request "modify_search".
        # Once the backend has anchored a different broker frame, preserving
        # follow_up would immediately route back through the old direction.
        route_intent = (
            "search_job" if resolved_frame == "job_search" else "search_worker"
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
