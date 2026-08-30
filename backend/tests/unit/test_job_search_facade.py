"""Phase 2-3 worker job-search facade contracts."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.listing.render import render_listing_card, render_listing_cards
from app.listing.search import JobSearchFacade, SearchTurn, apply_criteria_patch, scrub_listing_text
from app.listing.search import FacadeResult
from app.models import ContactRequest
from app.schemas.conversation import CandidateSnapshot, SessionState
from app.schemas.search import ListingCard
from app.services.user_service import UserContext


def _worker(**overrides):
    values = dict(
        external_userid="worker-1", role="worker", status="active",
        display_name=None, company=None, contact_person=None, phone="13800000000",
        can_search_jobs=True, can_search_workers=False,
        is_first_touch=False, should_welcome=False,
    )
    values.update(overrides)
    return UserContext(**values)


def test_listing_card_rejects_pii_and_internal_fields():
    with pytest.raises(ValueError):
        ListingCard(
            listing_id="recruitment.job:1", title="岗位", contact_action="联系",
            attributes={"phone": "13800000000"},
        )
    with pytest.raises(ValueError):
        ListingCard(
            listing_id="recruitment.job:1", title="岗位", contact_action="联系",
            attributes={"internal_sql": "SELECT * FROM job"},
        )


def test_renderer_is_fixed_and_does_not_render_phone():
    card = ListingCard(
        listing_id="recruitment.job:1", listing_ref="recruitment.job:1",
        title="甲厂 | 普工", body_summary="长白班", location_text="苏州市",
        attributes={"salary": "5500元/月", "benefits": ["包吃"]},
        contact_action="回复“联系”获取进一步沟通入口",
        contact_request_id="cr_opaque",
    )
    text = render_listing_cards([card], has_more=True)
    assert "甲厂 | 普工" in text
    assert "5500元/月" in text
    assert "cr_opaque" not in text
    assert "13800000000" not in text
    assert "更多" in text
    assert render_listing_card(card).startswith("甲厂 | 普工")


def test_description_pii_is_scrubbed_before_card_and_renderer():
    assert "13800000000" not in scrub_listing_text("联系 13800000000，微信: jobbridge88")
    assert "jobbridge88" not in scrub_listing_text("联系 13800000000，微信: jobbridge88")
    card = ListingCard(
        listing_id="recruitment.job:2", title="岗位",
        body_summary=scrub_listing_text("长白班，电话 13800000000，微信 wxid_job88"),
        contact_action="回复“联系”获取进一步沟通入口",
    )
    rendered = render_listing_card(card)
    assert "13800000000" not in card.body_summary
    assert "wxid_job88" not in card.body_summary
    assert "13800000000" not in rendered
    assert "wxid_job88" not in rendered


def test_apply_criteria_patch_is_explicit_and_deterministic():
    result = apply_criteria_patch(
        {"city": ["苏州"], "job_category": ["普工"], "salary_floor_monthly": 6000},
        [
            {"op": "add", "field": "city", "value": "成都"},
            {"op": "update", "field": "salary_floor_monthly", "value": 7000},
            {"op": "remove", "field": "job_category", "value": "普工"},
        ],
    )
    assert result == {"city": ["苏州", "成都"], "salary_floor_monthly": 7000}
    with pytest.raises(Exception):
        apply_criteria_patch({}, [{"op": "replace", "field": "city", "value": "苏州"}])
    with pytest.raises(ValueError):
        apply_criteria_patch({}, [{"op": "update", "field": "owner_userid", "value": "bad"}])


def test_facade_legacy_adapter_preserves_result_and_adds_cards(monkeypatch):
    result = SimpleNamespace(reply_text="legacy", result_count=1, has_more=False)
    outcome = SimpleNamespace(direction="search_job")
    legacy = MagicMock()
    legacy.search_jobs.return_value = (result, outcome)
    facade = JobSearchFacade(MagicMock(), enabled=True, legacy_service=legacy)
    session = SessionState(
        role="worker",
        candidate_snapshot=CandidateSnapshot(
            candidate_ids=["1"], snapshot_id="snap-1", direction="search_job",
        ),
        shown_items=["1"],
    )
    db = MagicMock()
    job = {"id": 1, "city": "苏州", "job_category": "普工", "description": "desc"}
    monkeypatch.setattr("app.services.search_service._validate_job_ids", lambda ids, db: [SimpleNamespace(id=1)])
    monkeypatch.setattr("app.services.search_service._jobs_to_dicts", lambda jobs, db: [job])
    monkeypatch.setattr("app.services.search_service._visibility_snapshot", lambda *args: SimpleNamespace())
    monkeypatch.setattr("app.services.permission_service.filter_jobs_batch", lambda jobs, role, vis: jobs)
    response = facade.search_jobs_v1(_worker(), {"city": ["苏州"]}, session, SearchTurn("苏州普工"), db=db)
    assert response.result is result
    assert response.outcome is outcome
    assert response.used_facade is True
    assert response.cards[0].listing_ref == "recruitment.job:1"
    assert response.cards[0].contact_request_id.startswith("cr_")
    legacy.search_jobs.assert_called_once()


def test_facade_persists_contact_request_for_each_real_card(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[ContactRequest.__table__])
    db = Session(engine)
    result = SimpleNamespace(reply_text="legacy", result_count=1, has_more=False)
    outcome = SimpleNamespace(direction="search_job")
    legacy = MagicMock()
    legacy.search_jobs.return_value = (result, outcome)
    facade = JobSearchFacade(db, enabled=True, legacy_service=legacy)
    session = SessionState(
        role="worker",
        candidate_snapshot=CandidateSnapshot(
            candidate_ids=["1"], snapshot_id="snap-contact", direction="search_job",
        ),
        shown_items=["1"],
    )
    job = {"id": 1, "version": 7, "city": "苏州", "job_category": "普工", "description": "desc"}
    monkeypatch.setattr("app.services.search_service._validate_job_ids", lambda ids, db: [SimpleNamespace(id=1)])
    monkeypatch.setattr("app.services.search_service._jobs_to_dicts", lambda jobs, db: [job])
    monkeypatch.setattr("app.services.search_service._visibility_snapshot", lambda *args: SimpleNamespace())
    monkeypatch.setattr("app.services.permission_service.filter_jobs_batch", lambda jobs, role, vis: jobs)

    response = facade.search_jobs_v1(
        _worker(), {"city": ["苏州"]}, session, SearchTurn("苏州普工"), db=db,
    )

    request = db.query(ContactRequest).one()
    assert response.cards[0].contact_request_id == request.request_id
    assert request.actor_id == "worker-1"
    assert request.listing_ref == "recruitment.job:1"
    assert request.listing_version == 7


def test_legacy_adapter_retries_only_for_old_signature():
    result = SimpleNamespace(reply_text="legacy", result_count=0, has_more=False)
    outcome = SimpleNamespace(direction="search_job")

    class OldProvider:
        def __init__(self):
            self.calls = []

        def search_jobs(self, criteria, raw_query, session, actor, db, user_msg_id=None):
            self.calls.append(user_msg_id)
            return result, outcome

    provider = OldProvider()
    facade = JobSearchFacade(MagicMock(), enabled=False, legacy_service=provider)
    response = facade.search_jobs_v1(
        _worker(), {}, SessionState(role="worker"),
        SearchTurn("苏州岗位", "m-old"), db=MagicMock(),
        experience_flags=SimpleNamespace(),
    )
    assert response.result is result
    assert provider.calls == ["m-old"]


def test_facade_rejects_non_worker_or_wrong_profile():
    facade = JobSearchFacade(MagicMock(), enabled=True, legacy_service=MagicMock())
    with pytest.raises(PermissionError):
        facade.search_jobs_v1(
            _worker(role="factory"), {}, SessionState(role="factory"), SearchTurn(), db=MagicMock(),
        )
    with pytest.raises(PermissionError):
        facade.search_jobs_v1(
            _worker(), {}, SessionState(role="worker", profile="secondhand.item"), SearchTurn(), db=MagicMock(),
        )


def test_disabled_facade_returns_legacy_without_cards():
    result = SimpleNamespace(reply_text="legacy", result_count=0, has_more=False)
    legacy = MagicMock()
    legacy.search_jobs.return_value = (result, SimpleNamespace(direction="search_job"))
    facade = JobSearchFacade(MagicMock(), enabled=False, legacy_service=legacy)
    response = facade.search_jobs_v1(_worker(), {}, SessionState(role="worker"), SearchTurn(), db=MagicMock())
    assert response.used_facade is False
    assert response.fallback_reason == "disabled"
    assert response.cards == []


def test_relax_search_requires_confirmation_and_uses_original_criteria(monkeypatch):
    facade = JobSearchFacade(MagicMock(), enabled=False)
    session = SessionState(role="worker", search_criteria={"city": ["苏州"], "salary_floor_monthly": 6000})
    with pytest.raises(PermissionError):
        facade.relax_search(_worker(), session, SearchTurn("苏州岗位"), "relax_salary_10pct", db=MagicMock())
    captured = {}
    flags = SimpleNamespace(soft_preference_ranking=True)
    def execute(criteria, step, **kwargs):
        captured["criteria"] = criteria
        captured["step"] = step
        captured["experience_flags"] = kwargs.get("experience_flags")
        return SimpleNamespace(reply_text="legacy", result_count=0, has_more=False), SimpleNamespace(direction="search_job")
    monkeypatch.setattr("app.services.search_service.execute_relaxed_search", execute)
    session.pending_relaxation = {
        "direction": "search_job", "step": "relax_salary_10pct",
        "original_criteria": {"city": ["苏州"], "salary_floor_monthly": 6000},
    }
    facade.relax_search(_worker(), session, SearchTurn("苏州岗位"), "relax_salary_10pct", db=MagicMock(), confirmed=True, experience_flags=flags)
    assert captured == {"criteria": {"city": ["苏州"], "salary_floor_monthly": 6000}, "step": "relax_salary_10pct", "experience_flags": flags}


def test_relax_search_rejects_unregistered_step_or_pending_context(monkeypatch):
    facade = JobSearchFacade(MagicMock(), enabled=False)
    session = SessionState(
        role="worker", search_criteria={"city": ["苏州"]},
        pending_relaxation={
            "direction": "search_job", "step": "relax_salary_10pct",
            "original_criteria": {"city": ["苏州"]},
        },
    )
    with pytest.raises(ValueError):
        facade.relax_search(_worker(), session, SearchTurn(), "drop_salary", db=MagicMock(), confirmed=True)
    session.pending_relaxation["original_criteria"] = {"city": ["成都"]}
    with pytest.raises(PermissionError):
        facade.relax_search(_worker(), session, SearchTurn(), "relax_salary_10pct", db=MagicMock(), confirmed=True)


def test_router_show_more_uses_facade_action_when_enabled(monkeypatch):
    from app.services import message_router
    from app.wecom.callback import WeComMessage
    session = SessionState(
        role="worker",
        candidate_snapshot=CandidateSnapshot(candidate_ids=["1"], direction="search_job"),
        shown_items=[],
    )
    raw_result = SimpleNamespace(reply_text="cards", result_count=0, has_more=False)
    raw_outcome = SimpleNamespace(direction="search_job", snapshot_exhausted=False,
                                  initial_count=0, final_count=0, desired_count=3,
                                  applied_relax_step=None, soft_pref_hits={})
    facade_result = FacadeResult(raw_result, raw_outcome, [], used_facade=True)
    facade = MagicMock()
    facade.show_more.return_value = facade_result
    monkeypatch.setattr(message_router, "_job_search_facade_enabled", lambda user: True)
    monkeypatch.setattr(message_router, "_post_search_dispatch", lambda **kwargs: [])
    monkeypatch.setattr("app.listing.search.JobSearchFacade", lambda *args, **kwargs: facade)
    legacy_more = MagicMock(side_effect=AssertionError("router bypassed facade"))
    monkeypatch.setattr(message_router.search_service, "show_more", legacy_more)
    replies = message_router._handle_show_more(
        WeComMessage(msg_id="m-more", from_user="worker-1", content="更多"),
        _worker(), session, MagicMock(),
    )
    assert replies == []
    facade.show_more.assert_called_once()
    assert facade.show_more.call_args.kwargs["experience_flags"] is not None
    turn = facade.show_more.call_args.args[2]
    assert turn.snapshot_id is None
    assert turn.page_size == 3


def test_router_records_fallback_when_facade_is_disabled(monkeypatch):
    from app.services import message_router
    from app.wecom.callback import WeComMessage

    session = SessionState(role="worker")
    result = SimpleNamespace(reply_text="legacy", result_count=0, has_more=False)
    outcome = SimpleNamespace(
        direction="search_job", snapshot_exhausted=False, initial_count=0,
        final_count=0, desired_count=3, applied_relax_step=None,
        soft_pref_hits={},
    )
    monkeypatch.setattr(message_router.search_service, "resolve_show_more_direction", lambda *_: "search_job")
    monkeypatch.setattr(message_router.search_service, "show_more", lambda *args, **kwargs: (result, outcome))
    monkeypatch.setattr(message_router, "_job_search_facade_enabled", lambda *_: False)
    monkeypatch.setattr(message_router, "_experience_flags_for", lambda *args, **kwargs: SimpleNamespace())
    events = []
    monkeypatch.setattr(message_router, "log_event", lambda event, **fields: events.append((event, fields)))
    monkeypatch.setattr(message_router, "_post_search_dispatch", lambda **kwargs: [])

    message_router._handle_show_more(
        WeComMessage(msg_id="m-disabled", from_user="worker-1", content="更多"),
        _worker(), session, MagicMock(),
    )
    fallback = [fields for event, fields in events if event == "facade_fallback"]
    assert fallback
    assert fallback[-1]["action"] == "show_more"
    assert fallback[-1]["reason"] in {"disabled", "out_of_bucket"}


def test_facade_show_more_projection_failure_does_not_repeat_snapshot_consume(monkeypatch):
    legacy_result = SimpleNamespace(reply_text="legacy page", result_count=1, has_more=False)
    outcome = SimpleNamespace(direction="search_job")
    service = MagicMock()
    service.show_more.return_value = (legacy_result, outcome)
    facade = JobSearchFacade(MagicMock(), enabled=True, legacy_service=service)
    session = SessionState(role="worker", shown_items=["1"])
    monkeypatch.setattr(facade, "cards_for_snapshot", MagicMock(side_effect=RuntimeError("projection")))
    response = facade.show_more(_worker(), session, db=MagicMock())
    assert response.result is legacy_result
    assert response.used_facade is False
    assert response.fallback_reason == "card_projection_failed"
    service.show_more.assert_called_once()


def test_show_more_passes_experience_flags_to_legacy_provider_with_compatibility():
    result = SimpleNamespace(reply_text="page", result_count=0, has_more=False)
    outcome = SimpleNamespace(direction="search_job")

    class OldProvider:
        def __init__(self):
            self.calls = 0

        def show_more(self, session, actor, db):
            self.calls += 1
            return result, outcome

    provider = OldProvider()
    facade = JobSearchFacade(MagicMock(), enabled=False, legacy_service=provider)
    response = facade.show_more(
        _worker(), SessionState(role="worker"), SearchTurn("更多", "m-more"),
        db=MagicMock(), experience_flags=SimpleNamespace(soft_preference_ranking=True),
    )
    assert response.result is result
    assert response.used_facade is False
    assert response.fallback_reason == "disabled"
    assert provider.calls == 1


def test_relax_search_passes_experience_flags_to_old_provider(monkeypatch):
    result = SimpleNamespace(reply_text="relaxed", result_count=0, has_more=False)
    outcome = SimpleNamespace(direction="search_job")
    captured = {}

    def old_execute(criteria, step, *, direction, raw_query, session, user_ctx, db, user_msg_id=None):
        captured.update(criteria=criteria, step=step, user_msg_id=user_msg_id)
        return result, outcome

    monkeypatch.setattr("app.services.search_service.execute_relaxed_search", old_execute)
    facade = JobSearchFacade(MagicMock(), enabled=False)
    session = SessionState(
        role="worker", search_criteria={"city": ["苏州"]},
        last_criteria={"city": ["苏州"]},
        pending_relaxation={
            "direction": "search_job", "step": "relax_salary_10pct",
            "original_criteria": {"city": ["苏州"]},
        },
    )
    response = facade.relax_search(
        _worker(), session, SearchTurn("确认", "m-relax"),
        "relax_salary_10pct", db=MagicMock(), confirmed=True,
        experience_flags=SimpleNamespace(),
    )
    assert response.result is result
    assert response.used_facade is False
    assert captured == {"criteria": {"city": ["苏州"]}, "step": "relax_salary_10pct", "user_msg_id": "m-relax"}


def test_facade_out_of_bucket_returns_legacy_without_cards_or_projection(monkeypatch):
    result = SimpleNamespace(reply_text="legacy", result_count=1, has_more=False)
    outcome = SimpleNamespace(direction="search_job")
    legacy = MagicMock()
    legacy.search_jobs.return_value = (result, outcome)
    monkeypatch.setattr("app.listing.search.settings.job_search_facade_enabled", True)
    monkeypatch.setattr("app.listing.search.settings.job_search_facade_rollout_percentage", 0)
    facade = JobSearchFacade(MagicMock(), legacy_service=legacy)
    monkeypatch.setattr(facade, "cards_for_snapshot", MagicMock(side_effect=AssertionError("must not project")))
    response = facade.search_jobs_v1(_worker(), {}, SessionState(role="worker"), SearchTurn("q"), db=MagicMock())
    assert response.result is result
    assert response.cards == []
    assert response.used_facade is False
    assert response.fallback_reason == "out_of_bucket"


def test_facade_timeout_returns_single_legacy_result_without_rerank(monkeypatch):
    import time as _time

    result = SimpleNamespace(reply_text="legacy", result_count=0, has_more=False)
    outcome = SimpleNamespace(direction="search_job")
    calls = []

    def slow_search(*args, **kwargs):
        calls.append(kwargs.get("experience_flags"))
        _time.sleep(0.01)
        return result, outcome

    legacy = MagicMock()
    legacy.search_jobs.side_effect = slow_search
    facade = JobSearchFacade(MagicMock(), enabled=True, legacy_service=legacy, timeout_ms=1)
    response = facade.search_jobs_v1(
        _worker(), {}, SessionState(role="worker"), SearchTurn("q"),
        db=MagicMock(), experience_flags=SimpleNamespace(),
    )
    assert response.result is result
    assert response.used_facade is False
    assert response.fallback_reason == "timeout"
    assert response.cards == []
    assert len(calls) == 1


def test_provider_internal_type_error_is_not_treated_as_signature_compatibility():
    class BrokenProvider:
        def search_jobs(self, *args, **kwargs):
            raise TypeError("provider ranking failed")

    facade = JobSearchFacade(MagicMock(), enabled=True, legacy_service=BrokenProvider())
    with pytest.raises(RuntimeError, match="job_search_facade_failed"):
        facade.search_jobs_v1(
            _worker(), {}, SessionState(role="worker"), SearchTurn("q"),
            db=MagicMock(), experience_flags=SimpleNamespace(),
        )


def test_show_more_and_relax_fallbacks_emit_telemetry(monkeypatch):
    events = []
    monkeypatch.setattr("app.listing.search.log_event", lambda event, **fields: events.append((event, fields)))
    result = SimpleNamespace(reply_text="legacy", result_count=0, has_more=False)
    outcome = SimpleNamespace(direction="search_job")
    service = MagicMock()
    service.show_more.return_value = (result, outcome)
    facade = JobSearchFacade(MagicMock(), enabled=False, legacy_service=service)
    facade.show_more(_worker(), SessionState(role="worker"), SearchTurn("更多", "m-more"), db=MagicMock(), experience_flags=SimpleNamespace())
    assert events[-1][0] == "facade_fallback"
    assert events[-1][1]["action"] == "show_more"
    assert events[-1][1]["reason"] == "disabled"

    monkeypatch.setattr("app.services.search_service.execute_relaxed_search", lambda *args, **kwargs: (result, outcome))
    session = SessionState(
        role="worker", search_criteria={"city": ["苏州"]}, last_criteria={"city": ["苏州"]},
        pending_relaxation={"direction": "search_job", "step": "relax_salary_10pct", "original_criteria": {"city": ["苏州"]}},
    )
    facade.relax_search(_worker(), session, SearchTurn("确认", "m-relax"), "relax_salary_10pct", db=MagicMock(), confirmed=True, experience_flags=SimpleNamespace())
    assert events[-1][1]["action"] == "relax_search"
    assert events[-1][1]["reason"] == "disabled"
