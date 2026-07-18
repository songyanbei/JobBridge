"""Phase 5 observability event shape tests."""
from types import SimpleNamespace

from app.schemas.conversation import CandidateSnapshot, SessionState
from app.schemas.search import SearchOutcome
from app.services import (
    message_router,
    post_search_applier,
    recommendation_experience_gate,
    search_service,
)
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


def _assert_event_shape(events, event_name, required_keys):
    matches = [payload for event, payload in events if event == event_name]
    assert len(matches) == 1
    payload = matches[0]
    assert required_keys <= payload.keys()
    assert FORBIDDEN_KEYS.isdisjoint(payload.keys())
    return payload


def _worker_context():
    return UserContext(
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
    assert {
        "external_userid_hash",
        "mode",
        "direction",
        "show_match_reasons",
        "build_shadow_reasons",
        "soft_preference_ranking",
        "soft_preference_reasons",
        "soft_preference_notice",
        "rollout_bucket",
    } <= payload.keys()
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
    assert {
        "external_userid_hash",
        "direction",
        "item_type",
        "explanation_count",
        "reason_kinds",
        "shadow_only",
    } <= payload.keys()
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
    assert {
        "external_userid_hash",
        "mode",
        "action",
        "decision",
        "reasoning",
        "direction",
        "snapshot_exhausted",
        "initial_count",
        "initial_visible_count",
        "final_count",
        "final_visible_count",
        "shown_count",
        "remaining_count_capped",
        "desired_count",
        "applied_relax_step",
        "soft_pref_hits",
    } <= payload.keys()
    assert payload["external_userid_hash"] != "user-secret-001"
    assert payload["decision"] == "show_results"
    assert payload["final_visible_count"] == 3
    assert payload["desired_count"] == 3
    assert FORBIDDEN_KEYS.isdisjoint(payload.keys())


def test_auto_relax_applied_log_has_required_fields(monkeypatch):
    events = []
    monkeypatch.setattr(
        search_service,
        "log_event",
        lambda event, **payload: events.append((event, payload)),
    )
    monkeypatch.setattr(search_service, "_query_jobs", lambda *_args: [])
    monkeypatch.setattr(search_service, "_get_config_int", lambda *_args: 3)

    search_service.execute_relaxed_search(
        {"city": ["苏州市"], "salary_floor_monthly": 6000},
        "relax_salary_10pct",
        direction="search_job",
        raw_query="苏州电子厂",
        session=SessionState(role="worker"),
        user_ctx=_worker_context(),
        db=SimpleNamespace(),
        original_visible_count=2,
    )

    payload = _assert_event_shape(
        events,
        "auto_relax_applied",
        {
            "external_userid_hash",
            "direction",
            "field",
            "original_visible_count",
            "relaxed_visible_count",
            "relaxed_shown_count",
            "applied",
        },
    )
    assert payload["original_visible_count"] == 2
    assert payload["relaxed_visible_count"] == 0


def test_show_more_exhausted_log_has_required_fields(monkeypatch):
    events = []
    monkeypatch.setattr(
        search_service,
        "log_event",
        lambda event, **payload: events.append((event, payload)),
    )
    monkeypatch.setattr(search_service, "_get_config_int", lambda *_args: 3)
    monkeypatch.setattr(
        search_service.conversation_service,
        "invalidate_snapshot_if_expired",
        lambda _session: False,
    )
    monkeypatch.setattr(
        search_service.conversation_service,
        "get_next_candidate_ids",
        lambda *_args: [],
    )
    session = SessionState(
        role="worker",
        active_flow="search_active",
        candidate_snapshot=CandidateSnapshot(
            candidate_ids=["1"],
            effective_criteria={"city": ["苏州市"]},
        ),
    )

    search_service.show_more(session, _worker_context(), SimpleNamespace())

    payload = _assert_event_shape(
        events,
        "show_more_exhausted",
        {
            "external_userid_hash",
            "direction",
            "remaining_count_capped",
            "snapshot_has_effective_criteria",
            "active_flow",
        },
    )
    assert payload["direction"] == "search_job"
    assert payload["snapshot_has_effective_criteria"] is True


def test_soft_preference_notice_log_has_required_fields(monkeypatch):
    events = []
    monkeypatch.setattr(
        post_search_applier,
        "log_event",
        lambda event, **payload: events.append((event, payload)),
    )
    outcome = SearchOutcome(
        direction="search_job",
        criteria_used={"city": ["苏州市"]},
        initial_count=3,
        final_count=3,
        desired_count=3,
        low_recall_threshold=3,
        visible_count=3,
        shown_count=3,
        soft_pref_hits={"provide_meal": 2},
    )
    ctx = SimpleNamespace(
        decision=SimpleNamespace(soft_pref_notice="已优先展示包吃岗位"),
        search_outcome=outcome,
        user_ctx=_worker_context(),
        experience_flags=SimpleNamespace(soft_preference_notice=True),
        search_result=SimpleNamespace(reply_text="results"),
    )

    assert post_search_applier._render_soft_pref_notice(ctx) == "已优先展示包吃岗位\n\nresults"
    payload = _assert_event_shape(
        events,
        "soft_preference_notice_shown",
        {
            "external_userid_hash",
            "direction",
            "soft_pref_hits",
            "soft_pref_fields",
            "visible_count",
            "shown_count",
            "notice_gate",
        },
    )
    assert payload["soft_pref_fields"] == ["provide_meal"]
