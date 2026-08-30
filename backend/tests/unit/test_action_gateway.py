"""Workstream A Gateway single-parse and fail-closed contracts."""
from types import SimpleNamespace

import pytest

from app.llm.base import IntentResult
from app.schemas.conversation import SessionState
from app.services import action_gateway
from app.services.action_gateway import ActionGateway
from app.wecom.callback import WeComMessage


def _msg(text: str, turn_id: str = "turn-1"):
    return WeComMessage(
        msg_id="msg-1", turn_id=turn_id, from_user="worker-1",
        msg_type="text", content=text,
    )


def _session(**values):
    return SessionState(profile="recruitment.job", role="worker", history=[], **values)


def _actor(role: str = "worker"):
    return SimpleNamespace(role=role)


def _route(intent: str, structured_data=None):
    return SimpleNamespace(
        intent_result=IntentResult(
            intent=intent, structured_data=structured_data or {}, confidence=1.0,
        ),
        parse_result=None,
    )


def test_off_does_not_parse_or_create_action(monkeypatch):
    calls = []
    monkeypatch.setattr(
        action_gateway.intent_service, "classify_for_action_gateway",
        lambda **kwargs: calls.append(kwargs),
    )

    envelope = ActionGateway(mode="off").classify(
        _msg("苏州找普工"), session=_session(), actor=_actor(),
    )

    assert calls == []
    assert envelope.action_name == "none"
    assert envelope.legacy_reason == "mode_off"
    assert envelope.parse_ref is None


@pytest.mark.parametrize("mode", ["shadow", "on"])
def test_shadow_and_on_parse_exactly_once(monkeypatch, mode):
    calls = []

    def classify(**kwargs):
        calls.append(kwargs)
        return _route("search_job", {"city": "苏州"})

    monkeypatch.setattr(action_gateway.intent_service, "classify_for_action_gateway", classify)
    envelope = ActionGateway(mode=mode).classify(
        _msg("苏州找普工"), session=_session(), actor=_actor(),
    )

    assert len(calls) == 1
    assert envelope.action_name == "search_job"
    assert envelope.parse_ref
    assert envelope.parse_digest and len(envelope.parse_digest) == 64
    assert envelope.request_digest and len(envelope.request_digest) == 64


def test_unknown_intent_is_not_guessed_as_supported_action(monkeypatch):
    monkeypatch.setattr(
        action_gateway.intent_service, "classify_for_action_gateway",
        lambda **kwargs: _route("not-a-real-intent"),
    )

    envelope = ActionGateway(mode="on").classify(
        _msg("随便说点什么"), session=_session(), actor=_actor(),
    )

    assert envelope.action_name == "unknown"
    assert envelope.is_supported is False


def test_classifier_failure_is_fail_closed_and_does_not_retry(monkeypatch):
    calls = []

    def fail_once(**kwargs):
        calls.append(kwargs)
        raise TimeoutError("provider timeout")

    monkeypatch.setattr(action_gateway.intent_service, "classify_for_action_gateway", fail_once)
    envelope = ActionGateway(mode="on").classify(
        _msg("苏州找普工"), session=_session(), actor=_actor(),
    )

    assert len(calls) == 1
    assert envelope.action_name == "unknown"
    assert envelope.legacy_reason == "classifier_error"
    assert envelope.parse_ref is None


def test_actor_and_turn_bind_request_digest(monkeypatch):
    monkeypatch.setattr(
        action_gateway.intent_service, "classify_for_action_gateway",
        lambda **kwargs: _route("search_job", {"city": "苏州"}),
    )
    gateway = ActionGateway(mode="on")
    first = gateway.classify(_msg("苏州找普工", "turn-a"), session=_session(), actor=_actor())
    second = gateway.classify(_msg("苏州找普工", "turn-b"), session=_session(), actor=_actor())
    non_worker = gateway.classify(
        _msg("苏州找普工", "turn-a"), session=_session(), actor=_actor("factory"),
    )

    assert first.turn_id == "turn-a"
    assert second.turn_id == "turn-b"
    assert first.request_digest != second.request_digest
    assert non_worker.action_name == "unknown"


def test_context_with_unsupported_action_is_fail_closed():
    envelope = ActionGateway._envelope("turn-1", "unknown")
    assert envelope.is_supported is False
    assert envelope.action_name not in {"search_job", "show_more_job", "relax_job"}
