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
