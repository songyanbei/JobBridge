"""阶段二兼容派生层（dialogue-intent-extraction-phased-plan §2.1.6 / current-state §6.2）。

把 DialogueDecision 派生回 legacy IntentResult，让现有 message_router._dispatch_intent
和 _route_* 不需要重写。新链路 dual-read 命中后仍走旧路由，把语义压平为 intent
+ structured_data + missing_fields，但保持「modify_search / answer_missing_slot →
follow_up + 全量 criteria 快照」语义，避免回到 criteria_patch op 歧义。
"""
from __future__ import annotations

import logging

from app.llm.base import IntentResult
from app.schemas.conversation import SessionState
from app.services.dialogue_reducer import DialogueDecision

logger = logging.getLogger(__name__)


def decision_to_intent_result(
    decision: DialogueDecision, session: SessionState,
) -> IntentResult:
    """把 DialogueDecision 派生为兼容 IntentResult。

    映射约定（current-state §6.2）：
    - start_search + idle/no-criteria → search_job/search_worker，structured_data=accepted_slots_delta
    - modify_search / answer_missing_slot + 已有 criteria → follow_up，structured_data=final_search_criteria
    - start_upload → upload_job / upload_resume
    - cancel / reset / resolve_conflict → command
    - show_more → show_more
    - chitchat → chitchat

    注意：
    - clarification 路径下 message_router 会在 compat 之前直接渲染反问文案；
      这里仍按主路径派生 intent，作为旁路兜底。
    - structured_data 在 follow_up 路径取 final_search_criteria（全量快照），
      避免还原 criteria_patch op 语义。
    """
    act = decision.dialogue_act
    frame = decision.resolved_frame
    accepted = dict(decision.accepted_slots_delta or {})
    final_criteria = dict(decision.final_search_criteria or {})
    has_existing = bool(session.search_criteria or {})

    if act == "start_upload":
        if frame == "job_upload":
            return IntentResult(
                intent="upload_job",
                structured_data=accepted,
                missing_fields=list(decision.missing_slots or []),
                confidence=0.9,
            )
        if frame == "resume_upload":
            return IntentResult(
                intent="upload_resume",
                structured_data=accepted,
                missing_fields=list(decision.missing_slots or []),
                confidence=0.9,
            )
        return IntentResult(intent="chitchat", confidence=0.0)

    if act == "start_search":
        if has_existing:
            # 已有 criteria 时 start_search 仍按 follow_up 处理，避免清旧条件
            return IntentResult(
                intent="follow_up",
                structured_data=final_criteria,
                missing_fields=list(decision.missing_slots or []),
                confidence=0.9,
            )
        if frame == "candidate_search":
            return IntentResult(
                intent="search_worker",
                structured_data=accepted,
                missing_fields=list(decision.missing_slots or []),
                confidence=0.9,
            )
        # job_search 默认
        return IntentResult(
            intent="search_job",
            structured_data=accepted,
            missing_fields=list(decision.missing_slots or []),
            confidence=0.9,
        )

    if act in {"modify_search", "answer_missing_slot"}:
        return IntentResult(
            intent="follow_up",
            structured_data=final_criteria,
            missing_fields=list(decision.missing_slots or []),
            confidence=0.9,
        )

    if act == "show_more":
        return IntentResult(intent="show_more", confidence=1.0)

    if act == "cancel":
        return IntentResult(
            intent="command",
            structured_data={"command": "cancel_pending"},
            confidence=1.0,
        )
    if act == "reset":
        return IntentResult(
            intent="command",
            structured_data={"command": "reset_search"},
            confidence=1.0,
        )
    if act == "resolve_conflict":
        # resolve_conflict 走 message_router 的冲突 handler，不走 _dispatch_intent。
        # 这里给一个安全 fallback，避免 message_router 拿到无 intent。
        return IntentResult(intent="command", confidence=1.0)

    return IntentResult(intent="chitchat", confidence=0.0)
