"""Phase 0 replay, one-step confirmation, and privacy contracts."""

import json

from tests.fixtures.dialogue_golden import (
    worker_relaxation_offer_accept,
    worker_shenzhen_chengdu_salary_shift,
    worker_show_more_exhausted_paginate,
)
from tests.fixtures.dialogue_golden.runner import assert_turn, run_dialogue_case


def _offline_ontology(monkeypatch):
    from app.services import intent_service
    monkeypatch.setattr(
        intent_service, "_get_city_lookup",
        lambda: {"深圳": "深圳市", "深圳市": "深圳市", "成都": "成都市", "成都市": "成都市"},
    )
    monkeypatch.setattr(
        intent_service, "_get_job_category_ontology",
        lambda: ({"普工": "普工"}, frozenset({"普工"})),
    )


def test_worker_multi_round_search_replay_smoke(monkeypatch):
    _offline_ontology(monkeypatch)
    case = worker_shenzhen_chengdu_salary_shift.CASE
    result = run_dialogue_case(case)
    assert len(result["turns"]) == 4
    for idx, turn in enumerate(case["turns"]):
        assert_turn(result["turns"][idx], turn["expect"], label=f"{case['id']}#{idx}")
    assert [call["salary_floor_monthly"] for call in result["spy"].jobs_calls] == [
        6000, 6000, 7000, 7000,
    ]
    assert result["session"].search_criteria["city"] == ["深圳市", "成都市"]


def test_one_relaxation_confirmation_executes_exactly_one_step(monkeypatch):
    _offline_ontology(monkeypatch)
    result = run_dialogue_case(worker_relaxation_offer_accept.CASE)
    assert len(result["spy"].relaxed_search_calls) == 1
    assert result["spy"].relaxed_search_calls[0]["step"] == "relax_salary_10pct"


def test_show_more_replay_is_cross_round_and_terminal_notice_is_stable(monkeypatch):
    _offline_ontology(monkeypatch)
    result = run_dialogue_case(worker_show_more_exhausted_paginate.CASE)
    assert result["turns"][0]["intent"] == "show_more"
    assert result["turns"][0]["reply_includes_paginate_header"] is True
    assert len(result["spy"].show_more_calls) == 1


def test_replay_observables_are_json_serializable_and_pii_free(monkeypatch):
    _offline_ontology(monkeypatch)
    result = run_dialogue_case(worker_shenzhen_chengdu_salary_shift.CASE)
    trace = json.dumps(result["turns"], ensure_ascii=False)
    # The replay trace is the contract surface; raw provider output and contact
    # identifiers must not be copied into it.
    for secret in ("13800000000", "wxid_", "jobbridge88", "phone"):
        assert secret not in trace
