import pytest

from app.llm.base import adapt_dialogue_parse


def test_adapter_rejects_slot_from_other_frame():
    with pytest.raises(ValueError, match="unknown dialogue slots"):
        adapt_dialogue_parse({
            "dialogue_act": "start_search",
            "frame_hint": "job_search",
            # resume_upload-only field; frame-flat schemas must stay isolated.
            "slots_delta": {"expected_cities": ["北京"]},
        })


def test_adapter_rejects_profile_mismatch():
    with pytest.raises(ValueError, match="unsupported dialogue profile"):
        adapt_dialogue_parse(
            {
                "dialogue_act": "chitchat",
                "frame_hint": "none",
            },
            profile="secondhand.item",
        )


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"schema_version": "dialogue.v9", "dialogue_act": "chitchat"}, "unsupported dialogue schema"),
        ({"dialogue_act": "future_act"}, "unknown dialogue_act"),
    ],
)
def test_adapter_rejects_unknown_protocol_values(payload, message):
    with pytest.raises(ValueError, match=message):
        adapt_dialogue_parse(payload)


def test_provider_parser_rejects_unknown_act_for_legacy_fallback():
    from app.core.exceptions import LLMParseError
    from app.llm.providers._base import parse_dialogue_response

    with pytest.raises(LLMParseError, match="dialogue_unknown_act"):
        parse_dialogue_response('{"dialogue_act":"future_act"}')


@pytest.mark.parametrize(
    "raw, message",
    [
        ('{"dialogue_act":"start_search","frame_hint":"future_frame"}', "dialogue_unknown_frame"),
        ('{"dialogue_act":"resolve_conflict","conflict_action":"future_action"}', "dialogue_invalid_conflict_action"),
        ('{"dialogue_act":"respond_relaxation_offer","relaxation_response":"maybe"}', "dialogue_invalid_relaxation_response"),
    ],
)
def test_provider_parser_rejects_unknown_action_fields(raw, message):
    from app.core.exceptions import LLMParseError
    from app.llm.providers._base import parse_dialogue_response

    with pytest.raises(LLMParseError, match=message):
        parse_dialogue_response(raw)
