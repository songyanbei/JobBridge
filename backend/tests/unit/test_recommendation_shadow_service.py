"""§7.5 shadow runner contracts: deadline, fail-closed limits and commit gate."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

from app.llm.base import RerankResult
from app.core.exceptions import LLMTimeout
from app.schemas.recommendation import RecommendationStrategyParameters
from app.services.recommendation_shadow_service import (
    ShadowJob,
    ShadowPolicy,
    ShadowRunner,
)
from app.services.search_service import _submit_shadow_candidate


class _FakeRedis:
    def __init__(self, *, fail: bool = False, global_capacity: bool = False):
        self.fail = fail
        self.global_capacity = global_capacity
        self.calls: list[str] = []

    async def eval(self, script, _keys, *_args):
        self.calls.append(script)
        if self.fail:
            raise ConnectionError("redis unavailable")
        if "ZCARD" in script:
            return 0 if self.global_capacity else 1
        if "current + reserve" in script:
            return 100
        return 1

    async def aclose(self):
        return None


class _FakeReranker:
    def __init__(self):
        self.calls = []

    async def arerank(self, **kwargs):
        self.calls.append(kwargs)
        return RerankResult(
            ranked_items=[
                {"id": "2", "score": 1.0},
                {"id": "1", "score": 0.8},
                {"id": "3", "score": 0.6},
            ],
            input_tokens=30,
            output_tokens=10,
        )


class _MissingUsageReranker(_FakeReranker):
    async def arerank(self, **kwargs):
        self.calls.append(kwargs)
        return RerankResult(
            ranked_items=[
                {"id": "2", "score": 1.0},
                {"id": "1", "score": 0.8},
                {"id": "3", "score": 0.6},
            ],
        )


class _TimeoutReranker(_FakeReranker):
    async def arerank(self, **kwargs):
        self.calls.append(kwargs)
        raise LLMTimeout()


def _job(
    *, deadline_offset: float = 3.0, raw_query: str = "北京普工，月薪六千",
) -> ShadowJob:
    now_mono = time.monotonic()
    candidates = (
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
    )
    return ShadowJob(
        request_id="req-shadow-1",
        direction="search_job",
        userid="viewer-1",
        raw_query=raw_query,
        role="worker",
        criteria={"city": ["北京市"], "salary_floor_monthly": 6000},
        query_digest="digest-1",
        candidate_dicts=candidates,
        precision_pool_ids=("1", "2", "3"),
        strategy_version=9,
        algorithm_version="recommendation-v1",
        parameters=RecommendationStrategyParameters.from_template(
            "balanced",
        ).model_dump(mode="json"),
        exposure_counts={},
        recent_exposures={},
        exposure_available=True,
        rotation_date="2026-07-27",
        scoring_time_utc=datetime.now(timezone.utc),
        deadline_monotonic=now_mono + deadline_offset,
        submitted_monotonic=now_mono,
        provider="fake",
        daily_token_limit=10_000,
    )


def test_deadline_is_measured_from_submit_and_expires_in_queue():
    reranker = _FakeReranker()
    runner = ShadowRunner(
        ShadowPolicy(local_concurrency=1, global_concurrency=1),
        reranker_factory=lambda: reranker,
        redis_factory=lambda: _FakeRedis(),
    )
    try:
        result = asyncio.run(runner._execute(_job(deadline_offset=-0.01)))
    finally:
        runner.shutdown()

    assert result.status == "timeout_in_queue"
    assert result.fallback == "deadline_in_queue"
    assert reranker.calls == []


def test_redis_failure_skips_shadow_before_provider_call():
    reranker = _FakeReranker()
    runner = ShadowRunner(
        ShadowPolicy(local_concurrency=1, global_concurrency=1),
        reranker_factory=lambda: reranker,
        redis_factory=lambda: _FakeRedis(fail=True),
    )
    try:
        result = asyncio.run(runner._execute(_job()))
    finally:
        runner.shutdown()

    assert result.status == "skipped_capacity"
    assert result.fallback == "redis_unavailable"
    assert reranker.calls == []


def test_shadow_uses_async_provider_with_zero_retries():
    reranker = _FakeReranker()
    job = replace(_job(), precision_pool_ids=("3", "1"))
    runner = ShadowRunner(
        ShadowPolicy(local_concurrency=1, global_concurrency=1),
        reranker_factory=lambda: reranker,
        redis_factory=lambda: _FakeRedis(),
    )
    try:
        result = asyncio.run(runner._execute(job))
    finally:
        runner.shutdown()

    assert result.status == "completed"
    assert set(result.top_ids) == {"1", "2", "3"}
    assert result.input_tokens == 30
    assert result.output_tokens == 10
    assert [item["id"] for item in reranker.calls[0]["candidates"]] == [3, 1]
    policy = reranker.calls[0]["call_policy"]
    assert policy.max_retries == 0
    assert policy.deadline_monotonic == job.deadline_monotonic


def test_token_reservation_is_pessimistic_for_chinese_prompts():
    job = _job(raw_query="北京普工，月薪六千，包吃住")
    reserve = ShadowRunner._token_reserve(job, max_output_tokens=64)
    serialized = json.dumps(
        {"query": job.raw_query, "candidates": list(job.candidate_dicts)},
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )

    assert reserve == len(serialized.encode("utf-8")) + 64
    assert reserve > len(serialized) // 4 + 64


def test_missing_provider_usage_is_persisted_at_pessimistic_budget():
    reranker = _MissingUsageReranker()
    job = _job()
    policy = ShadowPolicy(
        local_concurrency=1,
        global_concurrency=1,
        max_output_tokens=64,
    )
    runner = ShadowRunner(
        policy,
        reranker_factory=lambda: reranker,
        redis_factory=lambda: _FakeRedis(),
    )
    try:
        result = asyncio.run(runner._execute(job))
    finally:
        runner.shutdown()

    reserve = ShadowRunner._token_reserve(job, policy.max_output_tokens)
    assert result.status == "completed"
    assert result.input_tokens == reserve - policy.max_output_tokens
    assert result.output_tokens == policy.max_output_tokens


def test_provider_timeout_is_classified_and_billed_pessimistically():
    reranker = _TimeoutReranker()
    job = _job()
    policy = ShadowPolicy(
        local_concurrency=1,
        global_concurrency=1,
        max_output_tokens=64,
    )
    runner = ShadowRunner(
        policy,
        reranker_factory=lambda: reranker,
        redis_factory=lambda: _FakeRedis(),
    )
    try:
        result = asyncio.run(runner._execute(job))
    finally:
        runner.shutdown()

    assert result.status == "timeout"
    assert result.fallback == "deadline"
    assert result.input_tokens + result.output_tokens == ShadowRunner._token_reserve(
        job, policy.max_output_tokens,
    )


def test_persistence_waits_for_served_transaction_activation():
    persisted = []
    runner = ShadowRunner(
        ShadowPolicy(
            queue_capacity=2,
            local_concurrency=1,
            global_concurrency=1,
            persistence_threads=1,
            persistence_queue_capacity=1,
        ),
        reranker_factory=_FakeReranker,
        persistence_fn=lambda job, result, baseline: persisted.append(
            (job.request_id, result.status, baseline),
        ),
        redis_factory=lambda: _FakeRedis(),
    )
    job = _job()
    try:
        assert runner.submit(job) is not None
        deadline = time.time() + 2
        while time.time() < deadline:
            with runner._state_lock:
                state = runner._states.get(job.request_id)
                if state is not None and state.result is not None:
                    break
            time.sleep(0.01)
        assert persisted == []

        runner.set_served_baseline(job.request_id, ["1", "2", "3"])
        runner.activate_persistence(job.request_id)
        deadline = time.time() + 2
        while time.time() < deadline and not persisted:
            time.sleep(0.01)
    finally:
        runner.shutdown()

    assert persisted == [("req-shadow-1", "completed", ("1", "2", "3"))]


def test_search_submission_carries_full_production_ranking_inputs(monkeypatch):
    from app.services import recommendation_exposure_service
    from app.services import recommendation_shadow_service
    from app.services import recommendation_strategy_service

    captured = []
    monkeypatch.setattr(
        recommendation_strategy_service,
        "load_published_version",
        lambda _db, _version_id: SimpleNamespace(
            id=9,
            parameters=RecommendationStrategyParameters.from_template(
                "balanced",
            ).model_dump(mode="json"),
            algorithm_version="recommendation-v1",
        ),
    )
    monkeypatch.setattr(
        recommendation_exposure_service,
        "batch_candidate_exposures",
        lambda *_args, **_kwargs: {"1": 2},
    )
    monkeypatch.setattr(
        recommendation_exposure_service,
        "recent_user_exposures",
        lambda *_args, **_kwargs: {"1": datetime.now(timezone.utc)},
    )
    from app.services import recommendation_request_service
    monkeypatch.setattr(
        recommendation_request_service,
        "precision_pool",
        lambda *_args, **_kwargs: ["3", "1"],
    )
    monkeypatch.setattr(
        recommendation_shadow_service.shadow_runner,
        "submit",
        lambda job: captured.append(job) or SimpleNamespace(request_id=job.request_id),
    )
    assignment = SimpleNamespace(
        shadow_version_id=9,
        request_id="req-shadow-submit",
        snapshot_id="snap-shadow-submit",
        assignment=SimpleNamespace(
            model_copy=lambda update: SimpleNamespace(**update),
        ),
    )

    shared_scoring_time = datetime(2026, 7, 27, 15, 59, 59, tzinfo=timezone.utc)
    metadata = _submit_shadow_candidate(
        candidate_dicts=[{"id": 1}, {"id": 2}, {"id": 3}],
        direction="search_job",
        criteria={"city": ["北京市"]},
        userid="viewer-1",
        raw_query="北京普工",
        assignment_decision=assignment,
        db=SimpleNamespace(),
        request_now_utc=shared_scoring_time,
    )

    assert metadata["request_id"] == "req-shadow-submit"
    assert len(captured) == 1
    job = captured[0]
    assert job.criteria == {"city": ["北京市"]}
    assert job.precision_pool_ids == ("3", "1")
    assert [item["id"] for item in job.candidate_dicts] == [1, 2, 3]
    assert job.exposure_counts == {"1": 2}
    assert job.recent_exposures["1"].tzinfo is not None
    assert job.scoring_time_utc == shared_scoring_time
    assert job.rotation_date.count("-") == 2
    assert job.deadline_monotonic > job.submitted_monotonic
