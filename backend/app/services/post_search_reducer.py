"""Phase 5 结果感知二阶段裁决（phased-plan §5.0 / §5.1 / §5.2）。

post_search_reduce(ctx_or_kwargs) 是**纯函数**：
- 输入只读：DialogueParseResult / DialogueDecision / SessionState / SearchOutcome / role
- 输出：PostSearchDecision 声明式结果，不写 session、不调 LLM、不调 handler
- 所有副作用（包括二次检索、写 reply、清 pending_relaxation）由
  message_router._handle_text + post_search_applier 按 PostSearchDecision.action
  和 PostSearchContext.recursion_depth 执行。

5.0 子阶段：函数体直接返回 ``no_action``，不读取任何 outcome 字段；接通主链路
后行为逐字节等价旧路径（off / shadow 模式仍由 message_router 决定不调 reducer
或仅写日志）。

5.1 起会接通 paginate_no_more 分支；5.2 接通 0/低召回策略；5.3 接通软偏好排序；
5.4 接通可见性文案。

为避免 `app.services.search_service` 与本模块循环 import：
- 本模块**只**依赖 `app.schemas.search`（中立 DTO 模块）；
- 不 import `app.services.search_service`，二次检索由 message_router/applier 调度。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from app.config import settings
from app.schemas.conversation import SessionState
from app.schemas.search import SearchOutcome, SearchResult

if TYPE_CHECKING:
    # 仅类型提示用，避免运行时循环 import。
    from sqlalchemy.orm import Session

    from app.llm.base import DialogueParseResult
    from app.schemas.conversation import ReplyMessage  # noqa: F401
    from app.services.dialogue_reducer import DialogueDecision
    from app.services.user_service import UserContext
    from app.wecom.callback import WeComMessage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DTO 定义
# ---------------------------------------------------------------------------

class PostSearchDecision(BaseModel):
    """post_search_reduce 的裁决产物（phased-plan §5.0.1 第 1 项）。

    - 5.0 默认 action = ``no_action``，不影响 reply。
    - 5.1 起 reducer 可输出 ``paginate_no_more`` 等具体动作。
    """

    action: Literal[
        "show_results",
        "show_results_with_soft_pref_notice",
        "auto_relax_and_retry",
        "suggest_relaxation",
        "ask_clarification",
        "paginate_no_more",
        "no_action",
    ] = "no_action"
    """二阶段动作：决定 applier 如何处理 SearchResult.reply_text。"""

    relax_step: str | None = None
    """auto_relax_and_retry 时指定具体放宽步名（5.2 使用）。"""

    clarification: dict | None = None
    """ask_clarification 时的结构化文案参数（kind / question / fields ...）。"""

    suggested_directions: list[dict] = Field(default_factory=list)
    """suggest_relaxation / paginate_no_more 时给用户的方向列表
    （元素含 dimension / hint_text / target_field）。"""

    soft_pref_notice: str | None = None
    """show_results_with_soft_pref_notice 时的可见性文案（5.4 使用）。"""

    reasoning: str = ""
    """调试用，不进 reply；写日志。"""


@dataclass
class PostSearchContext:
    """post_search_applier 的统一上下文（phased-plan §5.0.1 第 5 项）。

    一次性定型，避免后续子阶段反复改 applier 签名。5.0 子阶段构造时
    所有字段必须由 message_router 一次性填齐（不允许 None 或缺失字段）。

    5.1 仅消费部分字段（decision / search_result / search_outcome / session / msg）；
    5.2 auto_relax_and_retry 路径会消费 db / user_ctx / raw_query / role；
    recursion_depth 防止 reducer 第二轮再输出 auto_relax_and_retry 死循环。
    """
    decision: PostSearchDecision
    search_result: SearchResult
    search_outcome: SearchOutcome
    parse_result: Any  # DialogueParseResult，避免 TYPE_CHECKING 引入运行时依赖
    dialogue_decision: Any  # DialogueDecision，同上
    session: SessionState
    msg: Any  # WeComMessage，同上
    user_ctx: Any  # UserContext，同上
    db: Any  # sqlalchemy.orm.Session，同上
    raw_query: str
    role: str
    recursion_depth: int = 0
    """0 = 主搜索路径；1 = 二阶段（auto_relax_and_retry 之后）；
    >=2 不允许（applier 端 assert 守护）。"""


# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------

def post_search_reduce(
    *,
    parse_result: "DialogueParseResult",
    decision: "DialogueDecision",
    session: SessionState,
    search_outcome: SearchOutcome,
    role: str,
) -> PostSearchDecision:
    """纯函数：基于搜索结果 + 阶段二裁决产出二阶段动作。

    5.0 子阶段：直接返回 ``no_action``，不读 outcome 字段，行为零变更。
    5.1 起接通 paginate_no_more（show_more 翻完时给具体降级建议）。
    5.2 起接通 auto_relax_and_retry / suggest_relaxation / ask_clarification。
    5.3 起接通 show_results_with_soft_pref_notice（5.4 才打文案）。

    入参全部只读，不允许写 session / 调 LLM / 调 handler；副作用由
    message_router + post_search_applier 按返回值执行。
    """
    # 5.1：show_more 翻完时输出 paginate_no_more + 具体降级建议方向。
    decision_phase5_1 = _decide_paginate_no_more(search_outcome, session)
    if decision_phase5_1 is not None:
        return decision_phase5_1

    # 5.2：低召回 / 0 结果策略（initial_count < low_recall_threshold）。
    decision_phase5_2 = _decide_zero_result(parse_result, decision, session, search_outcome)
    if decision_phase5_2 is not None:
        return decision_phase5_2

    # 5.3/5.4 接通点位（本子阶段不输出对应 action）。
    return PostSearchDecision(action="no_action", reasoning="phase 5.2 default")


# ---------------------------------------------------------------------------
# 决策分支（5.1）
# ---------------------------------------------------------------------------

def _decide_paginate_no_more(
    outcome: SearchOutcome,
    session: SessionState,
) -> PostSearchDecision | None:
    """phased-plan §5.1.1 第 2 项：show_more 翻完时输出 paginate_no_more。

    触发条件：``outcome.snapshot_exhausted=True``。其它条件返回 None 让上层走
    后续分支（5.2/5.3/5.4 在此追加）。

    suggested_directions 由 ``slot_schema.relaxation_directions(...)`` 渲染；
    若 criteria 已极简（directions 为空），applier 会兜底使用静态文案
    （phased-plan §失败模式落地）。
    """
    if not outcome.snapshot_exhausted:
        return None

    # 延迟 import 避免在 5.0 测试加载时引入 slot_schema 依赖
    from app.dialogue import slot_schema

    frame = (
        "candidate_search" if outcome.direction == "search_worker" else "job_search"
    )
    directions = slot_schema.relaxation_directions(
        session.search_criteria, frame=frame,
    )
    return PostSearchDecision(
        action="paginate_no_more",
        suggested_directions=directions,
        reasoning=f"snapshot_exhausted; {len(directions)} relaxation direction(s)",
    )


# 5.2 决策表只读取以下信号：
# - 当前 turn accepted_slots_delta（来自 decision.accepted_slots_delta）
# - session.awaiting_fields（仍在补槽 → 不放宽）
# - parse_result.confidence（< low_confidence_threshold → suggest_relaxation）
# - search_outcome.available_relax_steps（search_service 探查结果）
#
# **禁止**读取任何"历史 turn 的 dialogue_act 记忆"，phased-plan §5.2.4 验收 #9
# 用 grep 守护这条约束。

# search_service relax step 名 → 它会覆盖的 criteria 字段（用于"用户刚断言的字段
# 不要悄悄被覆盖"判定）。
_RELAX_STEP_TARGETS_JOB = {
    "relax_salary_10pct": frozenset({"salary_floor_monthly"}),
    "broaden_job_category": frozenset({"job_category"}),
    "drop_optional_filters": frozenset({"gender_required", "is_long_term", "age"}),
}
_RELAX_STEP_TARGETS_RESUME = {
    "relax_salary_10pct": frozenset({"salary_ceiling_monthly"}),
    "broaden_job_category": frozenset({"job_category"}),
    "drop_optional_filters": frozenset({"gender", "age"}),
}


def _decide_zero_result(
    parse_result: "DialogueParseResult",
    decision: "DialogueDecision",
    session: SessionState,
    outcome: SearchOutcome,
) -> PostSearchDecision | None:
    """phased-plan §5.2.1：低召回 / 0 结果策略决策表。

    触发条件：``initial_count < low_recall_threshold``（覆盖 0 结果与 1~2 条
    低召回，与 search_service.py:193 的 ``len(candidates) < top_n`` 行为一致）。

    决策优先级（与 phased-plan §5.2.1 第 2 项对齐）：
    1. 用户处于 awaiting_fields 中（仍在补槽）→ 返回 ``no_action`` 让用户先补齐
    2. 当前 turn 的 confidence < low_confidence_threshold → ``suggest_relaxation``
    3. 当前 turn accepted_slots_delta 触碰 relax 目标字段 → ``ask_clarification``
       反问（不悄悄覆盖用户刚断言的值）
    4. 默认 → ``auto_relax_and_retry`` 选第一个 available_relax_step
    5. 没有任何可用 step → ``no_action``（让 search_service 默认 fallback 接管，
       phased-plan §5.2.4 验收 #2 的"默认行为分支逐字节等价当前"）
    """
    if outcome.initial_count >= outcome.low_recall_threshold:
        return None

    # 规则 1：仍在补槽 → 不放宽
    if session.awaiting_fields:
        return PostSearchDecision(
            action="no_action",
            reasoning="user still in awaiting_fields; do not auto-relax",
        )

    # 规则 2：低置信度 → 给方向但不自动放宽
    threshold = settings.low_confidence_threshold
    if parse_result.confidence < threshold:
        return PostSearchDecision(
            action="suggest_relaxation",
            suggested_directions=_relax_steps_to_directions(outcome),
            reasoning=f"confidence {parse_result.confidence:.2f} < threshold {threshold}",
        )

    available = list(outcome.available_relax_steps or [])
    if not available:
        # 没有可用放宽方向：让 search_service 默认 fallback 接管（不输出非 no_action
        # 决策，phased-plan §失败模式表）。
        return PostSearchDecision(
            action="no_action",
            reasoning="no available relax steps; let legacy fallback handle",
        )

    # 规则 3：当前 turn slots_delta 触碰目标字段 → 反问
    delta_keys = set((decision.accepted_slots_delta or {}).keys())
    targets_table = (
        _RELAX_STEP_TARGETS_JOB if outcome.direction == "search_job"
        else _RELAX_STEP_TARGETS_RESUME
    )
    chosen_step = available[0]  # 默认采纳第一个可用 step
    targets = targets_table.get(chosen_step, frozenset())
    if delta_keys & targets:
        # 用户刚断言了 relax 目标字段，不悄悄覆盖；反问让用户主动同意。
        from app.dialogue import slot_schema
        human_label = slot_schema.relax_step_human_label(chosen_step)
        return PostSearchDecision(
            action="ask_clarification",
            relax_step=chosen_step,
            clarification={
                "kind": "relaxation_offer",
                "question": f"原条件没找到匹配，要{human_label}重新搜索吗？",
                "step": chosen_step,
            },
            reasoning=(
                f"current turn slots_delta touched {sorted(delta_keys & targets)}; "
                f"asking confirmation before applying step={chosen_step}"
            ),
        )

    # 规则 4：默认自动放宽（与现有 _run_*_fallback_steps 第一步等价）
    return PostSearchDecision(
        action="auto_relax_and_retry",
        relax_step=chosen_step,
        reasoning=f"default auto-relax; step={chosen_step}",
    )


def _relax_steps_to_directions(outcome: SearchOutcome) -> list[dict]:
    """把 available_relax_steps 转成 suggested_directions（同 paginate 结构）。"""
    from app.dialogue import slot_schema
    return [
        {
            "dimension": step,
            "hint_text": slot_schema.relax_step_human_label(step),
            "target_field": step,
        }
        for step in (outcome.available_relax_steps or [])
    ]
