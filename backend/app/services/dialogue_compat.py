"""阶段二兼容派生层（dialogue-intent-extraction-phased-plan §2.1.6 / current-state §6.2）。

把 DialogueDecision 派生回 legacy IntentResult，让现有 message_router._dispatch_intent
和 _route_* 不需要重写。新链路 dual-read 命中后仍走旧路由，把语义压平为 intent
+ structured_data + missing_fields，但保持「modify_search / answer_missing_slot →
follow_up + 全量 criteria 快照」语义，避免回到 criteria_patch op 歧义。
"""
from __future__ import annotations

import logging

from app.llm.base import (
    IntentResult,
    DialogueParseResult,
    VersionedDialogueParse,
)
from app.schemas.conversation import SessionState
from app.services.dialogue_reducer import DialogueDecision

logger = logging.getLogger(__name__)


def intent_to_dialogue_v1(
    intent: IntentResult,
    session: SessionState | None = None,
    *,
    profile: str = "recruitment.job",
) -> VersionedDialogueParse:
    """Explicitly map the legacy IntentResult into the closed v1 protocol.

    This is the only supported bridge from the legacy DTO to Runtime.  Unknown
    intents, commands, or payload shapes fail closed so callers can use the
    legacy route rather than guessing an Action.
    """
    name = intent.intent
    data = dict(intent.structured_data or {})
    frame = "none"
    act: str
    slots: dict = {}
    if name == "search_job":
        act, frame, slots = "start_search", "job_search", data
    elif name == "search_worker":
        act, frame, slots = "start_search", "candidate_search", data
    elif name in {"upload_job", "upload_and_search"}:
        act, frame, slots = "start_upload", "job_upload", data
    elif name == "upload_resume":
        act, frame, slots = "start_upload", "resume_upload", data
    elif name == "show_more":
        act = "show_more"
    elif name == "chitchat":
        act = "chitchat"
    elif name == "follow_up":
        awaiting = bool(session and getattr(session, "awaiting_fields", []))
        act = "answer_missing_slot" if awaiting else "modify_search"
        frame = (
            "job_search" if not session or getattr(session, "role", "worker") == "worker"
            else "candidate_search"
        )
        slots = data
    elif name == "command":
        command = str(data.get("command", "")).lower()
        if command in {"cancel", "cancel_pending", "abort"}:
            act = "cancel"
        elif command in {"reset", "reset_search"}:
            act = "reset"
        else:
            raise ValueError(f"unsupported legacy command: {command!r}")
    elif name in {"upload_conflict", "resolve_conflict"}:
        act = "resolve_conflict"
        action = data.get("conflict_action") or data.get("action")
        if action not in {"cancel_draft", "resume_pending_upload", "proceed_with_new"}:
            raise ValueError("legacy conflict result lacks a valid conflict_action")
        data = {"conflict_action": action}
    elif name in {"relaxation_answer", "respond_relaxation_offer"}:
        act = "respond_relaxation_offer"
        response = data.get("relaxation_response") or data.get("response")
        if response not in {"accept", "reject"}:
            raise ValueError("legacy relaxation result lacks accept/reject")
        data = {"relaxation_response": response}
    else:
        raise ValueError(f"unsupported legacy intent: {name!r}")

    parse = DialogueParseResult(
        dialogue_act=act,
        frame_hint=frame,
        slots_delta=slots,
        confidence=float(intent.confidence or 0.0),
        raw_response=intent.raw_response or "",
        **({"conflict_action": data["conflict_action"]} if act == "resolve_conflict" else {}),
        **({"relaxation_response": data["relaxation_response"]} if act == "respond_relaxation_offer" else {}),
    )
    return VersionedDialogueParse(schema_version="dialogue.v1", result=parse, profile=profile)


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
        # Reducer may deliberately reset an existing broker search when an explicit
        # object switches direction. Its route_intent is authoritative in that case.
        if decision.route_intent in {"search_job", "search_worker"}:
            return IntentResult(
                intent=decision.route_intent,
                structured_data=accepted,
                missing_fields=list(decision.missing_slots or []),
                confidence=0.9,
            )
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
        if (
            decision.route_intent in {"search_job", "search_worker"}
            and session.broker_direction in {"search_job", "search_worker"}
            and decision.route_intent != session.broker_direction
        ):
            return IntentResult(
                intent=decision.route_intent,
                structured_data=final_criteria,
                missing_fields=list(decision.missing_slots or []),
                confidence=0.9,
            )
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
    if act == "respond_relaxation_offer":
        # Phase 5 §5.2：与 resolve_conflict 同样走 message_router short-circuit
        # （_route_v2_relaxation_response），不进 _dispatch_intent；这里给安全
        # fallback 避免上游兜底报错。
        return IntentResult(intent="command", confidence=1.0)

    return IntentResult(intent="chitchat", confidence=0.0)
