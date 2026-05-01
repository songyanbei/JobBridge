"""阶段二 Decision Applier（dialogue-intent-extraction-phased-plan §2.1.2）。

reducer.reduce 是纯函数，不动 session；apply_decision 把 DialogueDecision 中的
state_transition / awaiting_ops / pending_interruption 物化到 SessionState。

所有冲突逻辑都复用 message_router._enter_upload_conflict（不重复实现）。
applier 只负责把声明式指令翻译成对 conversation_service / message_router 内
现成函数的调用。

调用顺序由 message_router._handle_text 决定：
1. 先调 reduce 得到 decision；
2. 如果 decision.clarification 非空，直接渲染反问，**不**调 applier；
3. 否则调 apply_decision，再走原 _route_*。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.llm.base import IntentResult
from app.schemas.conversation import SessionState
from app.services import conversation_service
from app.services.dialogue_reducer import DialogueDecision

logger = logging.getLogger(__name__)


@dataclass
class ApplyResult:
    """物化结果（便于 message_router 决定是否短路）。"""
    transition_executed: str = "none"
    enter_conflict_replies: list | None = None  # 仅 enter_upload_conflict 时填


def apply_awaiting_ops(decision: DialogueDecision, session: SessionState) -> None:
    """单独执行 awaiting_ops（不动 state_transition）。

    供 message_router 在 clarification / enter_upload_conflict 短路路径上调用，
    避免 awaiting_ops 被 short-circuit return 静默丢弃（adversarial review C1/I15）。
    """
    for op in decision.awaiting_ops or []:
        if op.get("op") == "consume":
            conversation_service.consume_search_awaiting(session, op.get("fields") or [])
        elif op.get("op") == "clear":
            conversation_service.clear_search_awaiting(session)


def apply_decision(
    decision: DialogueDecision,
    session: SessionState,
    *,
    msg=None,
    intent_result: IntentResult | None = None,
) -> ApplyResult:
    """把 decision 中的声明式指令落到 session。

    需要 msg / intent_result 仅当 state_transition=enter_upload_conflict 时
    （要调 _enter_upload_conflict 派生回复文案）。其它 transition 不依赖 msg。
    """
    transition = decision.state_transition

    # 1) awaiting_ops 先消费；与 transition 是正交的
    apply_awaiting_ops(decision, session)

    # 2) state_transition 翻译到具体 session 写入
    if transition == "none":
        return ApplyResult(transition_executed="none")

    if transition == "clear_awaiting":
        conversation_service.clear_search_awaiting(session)
        return ApplyResult(transition_executed="clear_awaiting")

    if transition == "reset_search":
        # 与 message_router 现有 reset 行为对齐：清搜索 criteria + awaiting + 快照
        session.search_criteria = {}
        session.candidate_snapshot = None
        session.shown_items = []
        conversation_service.clear_search_awaiting(session)
        return ApplyResult(transition_executed="reset_search")

    if transition == "clear_pending_upload":
        # 取消草稿：把上传状态清掉，回 idle
        session.pending_upload = {}
        session.pending_upload_intent = None
        session.awaiting_field = None
        session.pending_started_at = None
        session.pending_updated_at = None
        session.pending_expires_at = None
        session.pending_raw_text_parts = []
        session.active_flow = "idle"
        session.pending_interruption = None
        session.failed_patch_rounds = 0
        session.conflict_followup_rounds = 0
        return ApplyResult(transition_executed="clear_pending_upload")

    if transition == "resume_upload_collecting":
        # 恢复上传草稿：从 upload_conflict 退回 upload_collecting
        session.active_flow = "upload_collecting"
        session.pending_interruption = None
        session.conflict_followup_rounds = 0
        return ApplyResult(transition_executed="resume_upload_collecting")

    if transition == "exit_upload_conflict":
        # 不删 pending，仅退出冲突态（极少用，留给以后扩展）
        session.active_flow = "upload_collecting"
        session.pending_interruption = None
        return ApplyResult(transition_executed="exit_upload_conflict")

    if transition == "enter_upload_conflict":
        # 把 pending_interruption 落到 session；real reply 由 message_router 调用
        # _enter_upload_conflict 时生成，applier 这里只负责状态写入。
        if decision.pending_interruption:
            session.active_flow = "upload_conflict"
            session.pending_interruption = dict(decision.pending_interruption)
            session.conflict_followup_rounds = 0
        return ApplyResult(transition_executed="enter_upload_conflict")

    if transition == "apply_pending_interruption":
        # 取出 pending_interruption 作为新意图后续路由（具体由 message_router 处理）；
        # 这里只清状态机标记。
        session.active_flow = "idle"
        # 保留 pending_interruption 让 message_router 读完再清
        return ApplyResult(transition_executed="apply_pending_interruption")

    if transition == "enter_search_active":
        # 写入 final_search_criteria 到 session，进入 search_active；
        # 不清 awaiting（awaiting_ops 已经处理）。
        session.search_criteria = dict(decision.final_search_criteria or {})
        # 进入 search_active 让 message_router 后续 _route_search_active 走
        if session.search_criteria:
            session.active_flow = "search_active"
        return ApplyResult(transition_executed="enter_search_active")

    logger.warning("apply_decision: unknown state_transition=%s", transition)
    return ApplyResult(transition_executed="unknown")
