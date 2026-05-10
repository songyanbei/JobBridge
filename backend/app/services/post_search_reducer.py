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
    # 5.0 默认实现：不读取任何字段，直接返回 no_action。
    # 5.1+ 子阶段在这里加分支判断（按 phased-plan §5.1.1 / §5.2.1）。
    return PostSearchDecision(action="no_action", reasoning="phase 5.0 stub")
