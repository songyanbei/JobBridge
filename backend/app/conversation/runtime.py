"""Small adapter boundary for the versioned Dialogue runtime.

The runtime owns protocol adaptation and deterministic reduction.  It returns
declarative decisions; only the explicit ``apply`` method materializes a
decision into a SessionState.  Provider code therefore has no Session/DB write
capability, and legacy routing remains available to callers on any failure.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.llm import get_intent_extractor
from app.llm.base import (
    DialogueParseResult,
    VersionedDialogueParse,
    adapt_dialogue_parse,
)
from app.schemas.conversation import SessionState
from app.services import dialogue_applier, dialogue_compat, dialogue_reducer, intent_service


@dataclass(frozen=True)
class RuntimeResult:
    """Result of one runtime turn, including a legacy fallback reason."""

    parse: VersionedDialogueParse | None
    decision: dialogue_reducer.DialogueDecision | None
    route: intent_service.DialogueRouteResult
    fallback_reason: str | None = None


class DialogueRuntime:
    """Protocol/reducer adapter for a single Profile."""

    def __init__(self, *, profile: str = "recruitment.job") -> None:
        self.profile = profile

    def parse(
        self,
        text: str,
        role: str,
        *,
        history: list[dict] | None = None,
        session: SessionState | None = None,
    ) -> VersionedDialogueParse:
        if session is not None and session.profile != self.profile:
            raise ValueError("session profile does not match runtime profile")
        extractor = get_intent_extractor()
        raw = extractor.extract_dialogue(
            text=text,
            role=role,
            history=history,
            current_criteria=(dict(session.search_criteria) if session else None),
            session_hint=(intent_service.build_session_hint(session) if session else None),
        )
        return adapt_dialogue_parse(raw, profile=self.profile)

    def reduce(
        self,
        parse: VersionedDialogueParse | DialogueParseResult,
        session: SessionState,
        role: str,
        *,
        raw_text: str = "",
    ) -> dialogue_reducer.DialogueDecision:
        versioned = adapt_dialogue_parse(parse, profile=self.profile)
        return dialogue_reducer.reduce(versioned.result, session, role, raw_text=raw_text)

    def route(
        self,
        text: str,
        role: str,
        *,
        history: list[dict] | None = None,
        session: SessionState | None = None,
        userid: str | None = None,
        user_msg_id: str | None = None,
    ) -> RuntimeResult:
        """Route through the existing policy/rollout entry without new router branches."""
        if session is None:
            route = intent_service.classify_dialogue(text, role, history, userid=userid, user_msg_id=user_msg_id)
            return RuntimeResult(parse=None, decision=None, route=route, fallback_reason="no_session")
        try:
            route = intent_service.classify_dialogue(
                text, role, history, session=session, userid=userid, user_msg_id=user_msg_id,
            )
            if route.decision is not None and route.parse_result is not None:
                parse = adapt_dialogue_parse(route.parse_result, profile=self.profile)
                return RuntimeResult(parse=parse, decision=route.decision, route=route)
            return RuntimeResult(parse=None, decision=None, route=route)
        except Exception as exc:
            # classify_dialogue normally owns fallback; this final boundary keeps
            # callers from losing the turn if an adapter itself fails.
            legacy = intent_service._classify_intent_legacy(
                text=text, role=role, history=history,
                current_criteria=dict(session.search_criteria or {}),
                session_hint=intent_service.build_session_hint(session),
                user_msg_id=user_msg_id,
            )
            route = intent_service.DialogueRouteResult(
                intent_result=legacy, decision=None,
                source="runtime_fallback_legacy",
            )
            return RuntimeResult(parse=None, decision=None, route=route, fallback_reason=type(exc).__name__)

    def apply(
        self,
        result: RuntimeResult,
        session: SessionState,
        *,
        msg: Any = None,
        intent_result=None,
        db=None,
    ):
        """Materialize a declarative decision; never called by the LLM adapter."""
        if result.decision is None:
            return dialogue_applier.ApplyResult(transition_executed="none")
        return dialogue_applier.apply_decision(
            result.decision, session, msg=msg,
            intent_result=intent_result or result.route.intent_result, db=db,
        )


def run_turn(text: str, role: str, session: SessionState, **kwargs: Any) -> RuntimeResult:
    """Convenience entry point used by workers and replay tools."""
    return DialogueRuntime(profile=session.profile).route(text, role, session=session, **kwargs)

