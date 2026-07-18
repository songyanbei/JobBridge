"""Phase 5 observability event shape tests."""
from types import SimpleNamespace

from app.schemas.search import SearchOutcome
from app.services import message_router, recommendation_experience_gate, search_service
from app.services.recommendation_experience_gate import (
    RecommendationExperienceFlags,
    compute_recommendation_experience_flags,
)
from app.services.user_service import UserContext


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


def test_match_explanation_log_uses_hashed_userid(monkeypatch):
    events = []
    monkeypatch.setattr(
        search_service,
        "log_event",
        lambda event, **payload: events.append((event, payload)),
    )

    lines = search_service._build_job_reason_lines_by_id(
        [{"id": 1, "city": "苏州市", "job_category": "电子厂"}],
        {"city": ["苏州市"]},
        RecommendationExperienceFlags(build_shadow_reasons=True),
        external_userid_hash="hashed-user",
    )

    assert lines == {}
    assert events
    event, payload = events[0]
    assert event == "match_explanation_built"
    assert payload["external_userid_hash"] == "hashed-user"
    assert payload["shadow_only"] is True
    assert FORBIDDEN_KEYS.isdisjoint(payload.keys())


def test_post_search_decision_log_uses_hashed_userid(monkeypatch):
    events = []
    monkeypatch.setattr(
        message_router,
        "log_event",
        lambda event, **payload: events.append((event, payload)),
    )
    user_ctx = UserContext(
        external_userid="user-secret-001",
        role="worker",
        status="active",
        display_name=None,
        company=None,
        contact_person=None,
        phone=None,
        can_search_jobs=True,
        can_search_workers=False,
        is_first_touch=False,
        should_welcome=False,
    )
    outcome = SearchOutcome(
        direction="search_job",
        criteria_used={"city": ["苏州市"]},
        initial_count=4,
        final_count=4,
        desired_count=3,
        low_recall_threshold=3,
        visible_count=3,
        shown_count=3,
        remaining_count_capped=1,
        soft_pref_hits={"provide_meal": 2},
    )

    message_router._log_post_search_decision(
        mode="on",
        user_ctx=user_ctx,
        ps_decision=SimpleNamespace(action="show_results", reasoning="ok"),
        search_outcome=outcome,
    )

    assert events
    event, payload = events[0]
    assert event == "post_search_decision"
    assert "external_userid_hash" in payload
    assert payload["external_userid_hash"] != "user-secret-001"
    assert payload["decision"] == "show_results"
    assert payload["final_visible_count"] == 3
    assert payload["desired_count"] == 3
    assert FORBIDDEN_KEYS.isdisjoint(payload.keys())
