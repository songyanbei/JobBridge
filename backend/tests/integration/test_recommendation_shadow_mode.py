"""MySQL/Redis contracts for the recommendation shadow path."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from uuid import uuid4

import pytest
import redis.asyncio as async_redis

import app.models  # noqa: F401 - register all metadata
from app.config import settings
from app.db import Base, SessionLocal, engine
from app.models import RecommendationRequest, RecommendationSearchAttempt
from app.services.recommendation_shadow_service import (
    ShadowJob,
    ShadowPolicy,
    ShadowResult,
    ShadowRunner,
    _persist_shadow_result,
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)


def _job(request_id: str, *, daily_limit: int = 10_000) -> ShadowJob:
    now = datetime.now(timezone.utc)
    submitted = time.monotonic()
    return ShadowJob(
        request_id=request_id,
        direction="search_job",
        userid="integration-viewer",
        raw_query="integration query",
        role="worker",
        criteria={"city": ["Beijing"]},
        query_digest="digest",
        candidate_dicts=({"id": 1}, {"id": 2}),
        precision_pool_ids=("1", "2"),
        strategy_version=7,
        algorithm_version="recommendation-v1",
        parameters={},
        exposure_counts={},
        recent_exposures={},
        exposure_available=True,
        rotation_date="2026-07-27",
        scoring_time_utc=now,
        deadline_monotonic=submitted + 3,
        submitted_monotonic=submitted,
        provider="integration-provider",
        daily_token_limit=daily_limit,
    )


def test_redis_global_permit_and_daily_budget_are_atomic():
    async def _exercise():
        redis_client = async_redis.Redis.from_url(
            settings.redis_url, decode_responses=True,
        )
        runner = ShadowRunner(
            ShadowPolicy(global_concurrency=1, max_output_tokens=10),
            redis_factory=lambda: redis_client,
        )
        job = _job(str(uuid4()), daily_limit=25)
        permit_key = f"recommendation:shadow:permits:{job.provider}"
        budget_prefix = f"recommendation:shadow:token_budget:{job.direction}:"
        try:
            await redis_client.delete(permit_key)
            async for key in redis_client.scan_iter(f"{budget_prefix}*"):
                await redis_client.delete(key)

            token, key = await runner._acquire_global_permit(redis_client, job)
            assert token is not None
            denied, reason = await runner._acquire_global_permit(redis_client, job)
            assert denied is None
            assert reason == "global_capacity"
            await runner._release_global_permit(redis_client, key, token)

            first_ok, budget_key = await runner._reserve_budget(redis_client, job, 15)
            second_ok, reason = await runner._reserve_budget(redis_client, job, 15)
            assert first_ok is True
            assert second_ok is False
            assert reason == "token_budget"
            assert int(await redis_client.get(budget_key)) == 15
        finally:
            await redis_client.delete(permit_key)
            async for key in redis_client.scan_iter(f"{budget_prefix}*"):
                await redis_client.delete(key)
            await redis_client.aclose()
            runner.shutdown()

    asyncio.run(_exercise())


def test_mysql_shadow_result_updates_request_and_appends_attempt():
    request_id = str(uuid4())
    source_id = f"integration-shadow-{uuid4()}"
    db = SessionLocal()
    try:
        db.add(RecommendationRequest(
            request_id=request_id,
            source_inbound_msg_id=source_id,
            request_index=0,
            request_kind="initial",
            viewer_userid="integration-viewer",
            direction="search_job",
            query_digest="digest",
            execution_mode="shadow",
            served_assignment="legacy",
            candidate_strategy_version_id=7,
            algorithm_version="recommendation-v1",
            served_top_ids=["1", "3"],
        ))
        db.commit()

        _persist_shadow_result(
            _job(request_id),
            ShadowResult(
                status="completed",
                queue_wait_ms=4,
                latency_ms=19,
                top_ids=("1", "2"),
                input_tokens=20,
                output_tokens=5,
                candidate_ids=("1", "2"),
                precision_pool_ids=("1", "2"),
            ),
            ("1", "3"),
        )
        db.expire_all()

        request = db.get(RecommendationRequest, request_id)
        attempts = (
            db.query(RecommendationSearchAttempt)
            .filter(RecommendationSearchAttempt.request_id == request_id)
            .all()
        )
        assert request.shadow_status == "completed"
        assert request.shadow_top_ids == ["1", "2"]
        assert request.shadow_overlap_count == 1
        assert request.shadow_rank_delta == {"1": 0}
        assert request.shadow_input_tokens == 20
        assert len(attempts) == 1
        assert attempts[0].attempt_kind == "shadow_candidate"
        assert attempts[0].llm_status == "ok"
        assert attempts[0].llm_retry_count == 0
    finally:
        db.rollback()
        db.query(RecommendationSearchAttempt).filter(
            RecommendationSearchAttempt.request_id == request_id,
        ).delete(synchronize_session=False)
        db.query(RecommendationRequest).filter(
            RecommendationRequest.request_id == request_id,
        ).delete(synchronize_session=False)
        db.commit()
        db.close()
