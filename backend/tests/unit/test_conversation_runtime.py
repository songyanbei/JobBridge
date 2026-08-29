from types import SimpleNamespace

import pytest

from app.conversation.runtime import DialogueRuntime, RuntimeResult
from app.llm.base import DialogueParseResult, VersionedDialogueParse
from app.schemas.conversation import SessionState


def test_runtime_parse_wraps_legacy_provider_result(monkeypatch):
    class Extractor:
        def extract_dialogue(self, **kwargs):
            return DialogueParseResult(
                dialogue_act="start_search",
                frame_hint="job_search",
                slots_delta={"city": ["北京"]},
                confidence=0.9,
            )

    monkeypatch.setattr("app.conversation.runtime.get_intent_extractor", lambda: Extractor())
    parsed = DialogueRuntime().parse("北京找工作", "worker")
    assert isinstance(parsed, VersionedDialogueParse)
    assert parsed.schema_version == "dialogue.v1"
    assert parsed.dialogue_act == "start_search"


def test_runtime_rejects_mismatched_session_profile():
    session = SessionState(role="worker", profile="secondhand.item")
    with pytest.raises(ValueError, match="profile"):
        DialogueRuntime().parse("找工作", "worker", session=session)


def test_runtime_route_without_session_stays_legacy(monkeypatch):
    expected = SimpleNamespace(intent_result=SimpleNamespace(intent="chitchat"), decision=None, source="legacy")
    monkeypatch.setattr("app.conversation.runtime.intent_service.classify_dialogue", lambda *args, **kwargs: expected)
    result = DialogueRuntime().route("你好", "worker")
    assert isinstance(result, RuntimeResult)
    assert result.route is expected
    assert result.parse is None
    assert result.fallback_reason == "no_session"
