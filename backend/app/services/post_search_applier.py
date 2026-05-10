"""Phase 5 post_search_applier（phased-plan §5.0 / §5.1）。

负责把 ``PostSearchDecision`` 翻译成最终 ``ReplyMessage``，包括文案前缀拼接、
``paginate_no_more`` 模板渲染、``ask_clarification`` 渲染（桩，5.1 不会被
reducer 触发，仅 applier 端单测验证渲染）。

执行归属（phased-plan §5.2.1.5 表格）：
- 本 applier **仅**在搜索后链路里运行（_handle_search / _handle_follow_up /
  _handle_show_more）；
- 不处理 ``apply_relaxation / cancel_relaxation`` 这类用户确认放宽的
  state_transition——这些由 5.2 引入的 ``_route_v2_relaxation_response`` 接管；
- 5.0 接口契约一次性定型（``apply_post_search_decision(ctx)`` 接收
  ``PostSearchContext`` bundle），5.1+ 子阶段不再改签名。

5.1 子阶段实现的 action 分支：
- ``no_action`` → 直出 ``ctx.search_result.reply_text``；
- ``paginate_no_more`` → 用 ``slot_schema.relaxation_directions`` 渲染覆盖；
- ``ask_clarification`` → 桩实现（5.1 reducer 不会输出此 action，仅 applier 端
  单测构造 decision 验证渲染）；
- 其他 action（``auto_relax_and_retry / suggest_relaxation /
  show_results / show_results_with_soft_pref_notice``）→ fallback ``no_action``
  + 日志事件 ``post_search_unsupported_action``。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.schemas.conversation import ReplyMessage
from app.services.post_search_reducer import PostSearchContext

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

# Phase 5.1：ask_clarification 桩文案（5.2 才会被 reducer 真正触发）
_CLARIFICATION_DEFAULT_TEMPLATE = "需要确认一下：{question}"


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def apply_post_search_decision(
    ctx: PostSearchContext,
) -> list[ReplyMessage]:
    """根据 ``ctx.decision.action`` 决定是否覆盖 ``ctx.search_result.reply_text``。

    返回 ``list[ReplyMessage]`` 与 [_route_v2_resolve_conflict] 对齐，便于
    message_router 直接拼接。

    5.0/5.1 阶段所有路径都返回单元素列表；5.2 起 ``auto_relax_and_retry``
    会触发二次检索 + applier 第二轮，保留 list 类型支持后续多回复扩展。
    """
    action = ctx.decision.action
    msg_userid = ctx.msg.from_user

    if action == "no_action":
        return [_reply(msg_userid, ctx.search_result.reply_text)]

    if action == "paginate_no_more":
        return [_reply(msg_userid, _render_paginate_no_more(ctx))]

    if action == "ask_clarification":
        # 5.1 桩：reducer 不输出此 action（5.2 才会），applier 端单测可
        # 直接构造 decision 验证渲染。
        return [_reply(msg_userid, _render_ask_clarification(ctx))]

    # 5.1 未实现的 action：fallback no_action + 告警日志。
    # phased-plan §5.1.4 验收 #6：reducer 输出 show_results_with_soft_pref_notice
    # 时 applier fallback 到 no_action + 日志告警。
    logger.warning(
        "post_search_unsupported_action: action=%s 在 5.1 子阶段尚未实现，"
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
    """5.1 桩实现，5.2 接通后会从 ``ctx.decision.clarification`` 拿结构化文案参数。

    本子阶段：从 ``decision.clarification`` 取 ``question`` 字段渲染，缺失时
    使用 ``ctx.search_result.reply_text`` 兜底。
    """
    clar = ctx.decision.clarification or {}
    question = clar.get("question")
    if not question:
        # 5.1 reducer 不会输出 ask_clarification，只有桩输入才会到这里。
        return ctx.search_result.reply_text or "需要更多信息才能继续。"
    return _CLARIFICATION_DEFAULT_TEMPLATE.format(question=question)


# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------

def _reply(userid: str, content: str) -> ReplyMessage:
    """构造 ReplyMessage（与 message_router._reply 同结构，避免引入循环依赖）。"""
    return ReplyMessage(userid=userid, content=content)
