"""Phase 5 observability event shape tests."""
from app.services import recommendation_experience_gate
from app.services.recommendation_experience_gate import (
    compute_recommendation_experience_flags,
)


FORBIDDEN_KEYS = {
    "phone",
    "contact_person",
    "id_card",
    "wechat",
    "address",
    "raw_text",
    "userid",
}


def test_gate_log_uses_hashed_userid(monkeypatch):
    events = []
    monkeypatch.setattr(
        recommendation_experience_gate,
        "log_event",
        lambda event, **payload: events.append((event, payload)),
    )

    compute_recommendation_experience_flags(
        "user-secret-001",
        direction="search_job",
        mode="on",
        emit_log=True,
    )

    assert events
    event, payload = events[0]
    assert event == "recommendation_experience_gate"
    assert "external_userid_hash" in payload
    assert payload["external_userid_hash"] != "user-secret-001"
    assert FORBIDDEN_KEYS.isdisjoint(payload.keys())
