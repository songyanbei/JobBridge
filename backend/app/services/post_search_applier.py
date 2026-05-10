"""Phase 5 post_search_applier（phased-plan §5.0 / §5.1 / §5.2）。

负责把 ``PostSearchDecision`` 翻译成最终 ``ReplyMessage``。

执行归属（phased-plan §5.2.1.5 表格）：
- 本 applier **仅**在搜索后链路里运行（_handle_search / _handle_follow_up /
  _handle_show_more）；
- **不处理** ``apply_relaxation / cancel_relaxation`` 这类用户确认放宽的
  state_transition——这些由 ``_route_v2_relaxation_response`` 接管。

5.0/5.1/5.2 子阶段实现的 action 分支：
- ``no_action`` → 直出 ``ctx.search_result.reply_text``（所有阶段）；
- ``paginate_no_more`` → 用 ``slot_schema.relaxation_directions`` 渲染（5.1 起）；
- ``ask_clarification`` → 渲染反问 + **持久化 pending_relaxation**（5.2 起；
  5.1 是桩）；
- ``auto_relax_and_retry`` → 调 ``execute_relaxed_search`` + 二阶段 reducer
  + 自身递归一层（5.2 起，受 ctx.recursion_depth ≤ 1 守护）；
- ``suggest_relaxation`` → 渲染建议方向但不调二次检索（5.2 起）；
- 其他 action（``show_results / show_results_with_soft_pref_notice``）→ 5.4
  接通；本子阶段未实现时 fallback ``no_action`` + 日志事件
  ``post_search_unsupported_action``。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.schemas.conversation import ReplyMessage
from app.services.post_search_reducer import (
    PostSearchContext,
    post_search_reduce,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 文案模板（phased-plan §跨阶段共同约束 #3：clarification / 降级文案不依赖 LLM）
# ---------------------------------------------------------------------------

_PAGINATE_FALLBACK_TEMPLATE = "已经是所有匹配结果了。要不要换城市或工种重新搜索？"
"""criteria 已极简（relaxation_directions 为空）时的兜底文案
（phased-plan 失败模式表）。"""

_PAGINATE_HEADER = "已经是所有匹配结果了。可以试试以下方向重新搜索："

# Phase 5.2：ask_clarification 反问文案（reducer 已通过 clarification.question
# 给出业务文案；这里的模板仅用于桩输入或 question 缺失时的兜底）。
_CLARIFICATION_DEFAULT_TEMPLATE = "需要确认一下：{question}"

# Phase 5.2：suggest_relaxation 文案
_SUGGEST_RELAXATION_HEADER = "暂未找到匹配结果。可以试试以下方向重新搜索："

# Phase 5.2：cancel_relaxation 文案（_route_v2_relaxation_response 也可能复用）
_CANCEL_RELAXATION_REPLY = "好的，那我们换其他条件重新搜索。"

# Phase 5.2：pending_relaxation TTL（秒），与 search_awaiting_ttl_seconds 共享配置
# （phased-plan §5.2.3：TTL 复用 dialogue_policy.search_awaiting_ttl_seconds）。


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def apply_post_search_decision(
    ctx: PostSearchContext,
) -> list[ReplyMessage]:
    """根据 ``ctx.decision.action`` 决定是否覆盖 ``ctx.search_result.reply_text``。

    返回 ``list[ReplyMessage]`` 与 [_route_v2_resolve_conflict] 对齐，便于
    message_router 直接拼接。

    5.2 起 ``auto_relax_and_retry`` 会触发二次检索 + applier 第二轮，由
    ``ctx.recursion_depth`` 守护：仅允许 0 → 1 一层递归（assert）。
    """
    action = ctx.decision.action
    msg_userid = ctx.msg.from_user

    # 5.2 §跨阶段共同约束 #8：二阶段递归深度硬限制为 1
    assert ctx.recursion_depth <= 1, (
        f"post_search_applier recursion_depth={ctx.recursion_depth} > 1; "
        f"reducer should never produce auto_relax_and_retry on second pass"
    )

    if action == "no_action":
        return [_reply(msg_userid, ctx.search_result.reply_text)]

    if action == "paginate_no_more":
        return [_reply(msg_userid, _render_paginate_no_more(ctx))]

    if action == "ask_clarification":
        return _handle_ask_clarification(ctx)

    if action == "auto_relax_and_retry":
        return _handle_auto_relax_and_retry(ctx)

    if action == "suggest_relaxation":
        return [_reply(msg_userid, _render_suggest_relaxation(ctx))]

    # 5.4 接通后会处理 show_results / show_results_with_soft_pref_notice。
    # 本子阶段未实现的 action：fallback no_action + 告警日志（phased-plan
    # §5.1.4 验收 #6）。
    logger.warning(
        "post_search_unsupported_action: action=%s 在当前子阶段尚未实现，"
        "fallback no_action 直出 search_result.reply_text",
        action,
    )
    return [_reply(msg_userid, ctx.search_result.reply_text)]


# ---------------------------------------------------------------------------
# 渲染分支
# ---------------------------------------------------------------------------

def _render_paginate_no_more(ctx: PostSearchContext) -> str:
    """phased-plan §5.1.1 第 2 项：根据 suggested_directions 渲染降级文案。

    directions 来源于 ``slot_schema.relaxation_directions(...)``，每项含
    ``dimension / hint_text / target_field``。空 directions 时走兜底文案。
    """
    directions = ctx.decision.suggested_directions or []
    if not directions:
        return _PAGINATE_FALLBACK_TEMPLATE
    bullets = [f"- {d['hint_text']}" for d in directions]
    return _PAGINATE_HEADER + "\n" + "\n".join(bullets)


def _render_ask_clarification(ctx: PostSearchContext) -> str:
    """从 ``ctx.decision.clarification`` 拿结构化文案参数。

    5.2 起 reducer 通过 clarification.question 给出业务文案（reducer 已用
    ``slot_schema.relax_step_human_label`` 拼好），applier 直接用；缺失时
    使用 ``ctx.search_result.reply_text`` 兜底（5.1 桩输入路径）。
    """
    clar = ctx.decision.clarification or {}
    question = clar.get("question")
    if not question:
        return ctx.search_result.reply_text or "需要更多信息才能继续。"
    return question


def _handle_ask_clarification(ctx: PostSearchContext) -> list[ReplyMessage]:
    """phased-plan §5.2.3 post_search_applier 行 (b)：

    渲染反问文案 + **持久化** ``ctx.session.pending_relaxation``。结构与
    SessionState.pending_relaxation 注释一致；``raw_query / user_msg_id``
    是 P1 评审硬要求（确认轮 msg.content 不能用作二次 reranker query）。
    """
    from datetime import datetime, timedelta, timezone

    from app.config import settings as _settings_module

    clar = ctx.decision.clarification or {}
    step = ctx.decision.relax_step or clar.get("step") or ""

    # 计算 relaxed_criteria（仅用于审计 / 反问展示，不参与二次检索）
    if ctx.search_outcome.direction == "search_job":
        from app.services.search_service import _compute_relaxed_criteria_job
        relaxed = _compute_relaxed_criteria_job(
            ctx.search_outcome.criteria_used, step,
        )
    else:
        from app.services.search_service import _compute_relaxed_criteria_resume
        relaxed = _compute_relaxed_criteria_resume(
            ctx.search_outcome.criteria_used, step,
        )

    ttl = _settings_module.search_awaiting_ttl_seconds
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat()

    ctx.session.pending_relaxation = {
        "frame": (
            "candidate_search" if ctx.search_outcome.direction == "search_worker"
            else "job_search"
        ),
        "direction": ctx.search_outcome.direction,
        "step": step,
        "original_criteria": dict(ctx.search_outcome.criteria_used),
        "relaxed_criteria": dict(relaxed),
        # P1 评审 #2：必须持久化主搜索 raw_query + msg_id。确认轮 msg.content
        # 通常是"好的/可以"，不能用作 reranker query；user_msg_id 透传给
        # 二次 _rerank_with_logging 做归因（与主搜索一致）。
        "raw_query": ctx.raw_query,
        "user_msg_id": ctx.msg.msg_id,
        "expires_at": expires_at,
    }

    return [_reply(ctx.msg.from_user, _render_ask_clarification(ctx))]


def _handle_auto_relax_and_retry(
    ctx: PostSearchContext,
) -> list[ReplyMessage]:
    """phased-plan §5.2.3 post_search_applier 行 (a)：自动放宽 + 二次 reducer。

    内部步骤：
    1. 调 ``search_service.execute_relaxed_search(criteria_used, step, ...)``
       拿到二次检索的 ``(SearchResult, SearchOutcome)``；
    2. 构造新 ``PostSearchContext(recursion_depth=1, ...)``；
    3. 再调一次 ``post_search_reduce``（受 recursion_depth ≤ 1 守护）；
    4. 第二轮 reducer 不允许再输出 auto_relax_and_retry（assert 守护）；
    5. 把第二轮 applier 的回复返回。

    **第一参数必须是 ctx.search_outcome.criteria_used**（auto_relax_and_retry
    路径下，主搜索这一轮 search_service 还未自行放宽，criteria_used 即原始
    criteria，与 _route_v2_relaxation_response 路径里的 ``original_criteria``
    语义一致）。
    """
    from app.services.search_service import execute_relaxed_search

    step = ctx.decision.relax_step
    if not step:
        logger.warning(
            "auto_relax_and_retry without relax_step; fallback no_action"
        )
        return [_reply(ctx.msg.from_user, ctx.search_result.reply_text)]

    new_result, new_outcome = execute_relaxed_search(
        ctx.search_outcome.criteria_used,
        step,
        direction=ctx.search_outcome.direction,
        raw_query=ctx.raw_query,
        session=ctx.session,
        user_ctx=ctx.user_ctx,
        db=ctx.db,
        user_msg_id=ctx.msg.msg_id,
    )

    new_decision = post_search_reduce(
        parse_result=ctx.parse_result,
        decision=ctx.dialogue_decision,
        session=ctx.session,
        search_outcome=new_outcome,
        role=ctx.role,
    )
    # 第二轮 reducer 必须不再输出 auto_relax_and_retry（避免无限套娃）
    assert new_decision.action != "auto_relax_and_retry", (
        "post_search_reduce produced auto_relax_and_retry on second pass; "
        "this would cause infinite recursion"
    )

    new_ctx = PostSearchContext(
        decision=new_decision,
        search_result=new_result,
        search_outcome=new_outcome,
        parse_result=ctx.parse_result,
        dialogue_decision=ctx.dialogue_decision,
        session=ctx.session,
        msg=ctx.msg,
        user_ctx=ctx.user_ctx,
        db=ctx.db,
        raw_query=ctx.raw_query,
        role=ctx.role,
        recursion_depth=ctx.recursion_depth + 1,  # 0 → 1
    )
    return apply_post_search_decision(new_ctx)


def _render_suggest_relaxation(ctx: PostSearchContext) -> str:
    """phased-plan §5.2.1：低置信度 / 没有可放宽 step 时给方向不放宽。"""
    directions = ctx.decision.suggested_directions or []
    if not directions:
        # 没有方向也没有自动放宽——退回原始 reply（通常是 NO_*_MATCH_REPLY）
        return ctx.search_result.reply_text
    bullets = [f"- {d['hint_text']}" for d in directions]
    return _SUGGEST_RELAXATION_HEADER + "\n" + "\n".join(bullets)


# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------

def _reply(userid: str, content: str) -> ReplyMessage:
    """构造 ReplyMessage（与 message_router._reply 同结构，避免引入循环依赖）。"""
    return ReplyMessage(userid=userid, content=content)
