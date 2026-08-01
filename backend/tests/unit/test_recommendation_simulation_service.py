"""§8 strategy simulation: real LLM call, production inputs and no serving writes."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.llm.base import RerankResult
from app.schemas.recommendation import RecommendationStrategyParameters
from app.services.recommendation_simulation_service import simulate_strategy


def _candidates():
    return [
        {
            "id": 1,
            "owner_userid": "owner-a",
            "salary_floor_monthly": 5000,
            "salary_ceiling_monthly": 7000,
            "created_at": datetime.now(timezone.utc),
        },
        {
            "id": 2,
            "owner_userid": "owner-b",
            "salary_floor_monthly": 5500,
            "salary_ceiling_monthly": 7500,
            "created_at": datetime.now(timezone.utc),
        },
        {
            "id": 3,
            "owner_userid": "owner-c",
            "salary_floor_monthly": 6000,
            "salary_ceiling_monthly": 8000,
            "created_at": datetime.now(timezone.utc),
        },
    ]


def _draft():
    return SimpleNamespace(
        id=11,
        parameters=RecommendationStrategyParameters.from_template(
            "balanced",
        ).model_dump(mode="json"),
    )


def _patch_inputs(monkeypatch, rerank_result: RerankResult):
    from app.services import (
        recommendation_exposure_service,
        recommendation_request_service,
        search_service,
    )

    candidates = _candidates()
    monkeypatch.setattr(
        search_service,
        "_get_config_int",
        lambda key, *_args, **_kwargs: 50 if key == "match.max_candidates" else 3,
    )
    query_criteria = []

    def _query_jobs(criteria, *_args, **_kwargs):
        query_criteria.append(criteria)
        return [object()]

    monkeypatch.setattr(search_service, "_query_jobs", _query_jobs)
    monkeypatch.setattr(
        search_service,
        "_jobs_to_dicts",
        lambda *_args, **_kwargs: candidates,
    )
    monkeypatch.setattr(
        recommendation_exposure_service,
        "batch_candidate_exposures",
        lambda *_args, **_kwargs: {"1": 0, "2": 2, "3": 4},
    )
    monkeypatch.setattr(
        recommendation_exposure_service,
        "recent_user_exposures",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        recommendation_request_service,
        "precision_pool",
        lambda *_args, **_kwargs: ["2", "1", "3"],
    )
    calls = []

    def _rerank(**kwargs):
        calls.append(kwargs)
        return rerank_result

    monkeypatch.setattr(search_service, "_rerank_with_logging", _rerank)
    return candidates, calls, query_criteria


def test_simulation_invokes_v1_and_real_legacy_reranking(monkeypatch):
    candidates, calls, query_criteria = _patch_inputs(
        monkeypatch,
        RerankResult(
            ranked_items=[{"id": "2"}, {"id": "1"}, {"id": "3"}],
            input_tokens=22,
            output_tokens=8,
        ),
    )
    db = MagicMock()
    db.get.return_value = None

    result = simulate_strategy(
        db,
        draft=_draft(),
        direction="search_job",
        user_id="viewer-1",
        raw_query="找苏州电子厂，工资下限5500",
        criteria={
            "city": ["苏州"],
            "job_category": ["电子厂"],
            "salary_floor_monthly": 5500,
        },
    )

    assert result.llm_invoked is True
    assert result.semantic_source == "llm"
    assert result.simulation_mode == "llm"
    assert result.llm_input_tokens == 44
    assert result.llm_output_tokens == 16
    assert result.current_basis == "legacy"
    assert query_criteria == [{
        "city": ["苏州市"],
        "job_category": ["电子厂"],
        "salary_floor_monthly": 5500,
    }]
    assert [item.target_id for item in result.current_items] == [2, 1, 3]
    assert len(calls) == 2
    assert calls[0]["call_site"] == "recommendation_simulation"
    assert len(calls[0]["candidates"]) <= 20
    assert [
        str(item["id"]) for item in calls[0]["candidates"]
    ] == ["2", "1", "3"]
    assert calls[1]["call_site"] == "recommendation_simulation"
    assert [str(item["id"]) for item in calls[1]["candidates"]] == ["1", "2", "3"]
    assert len(result.draft_items) == 3
    assert result.candidates == candidates
    db.add.assert_not_called()
    db.commit.assert_not_called()


def test_simulation_labels_llm_failure_as_neutral_fallback(monkeypatch):
    _candidates_value, calls, _query_criteria = _patch_inputs(
        monkeypatch,
        RerankResult(ranked_items=[]),
    )
    db = MagicMock()
    db.get.return_value = None

    result = simulate_strategy(
        db,
        draft=_draft(),
        direction="search_job",
        user_id=None,
        raw_query="",
        criteria={"city": ["北京市"]},
    )

    assert len(calls) == 2
    assert result.llm_invoked is True
    assert result.semantic_source == "llm_fallback_neutral"
    assert result.simulation_mode == "deterministic_fallback"
    assert len(result.draft_items) == 3
