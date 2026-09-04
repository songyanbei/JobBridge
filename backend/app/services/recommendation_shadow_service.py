"""Asynchronous, bounded shadow ranking for recommendation-v1.

The serving thread only submits work and later activates persistence after its
request/outbox transaction commits.  Provider I/O runs on a dedicated asyncio
loop, while synchronous SQLAlchemy persistence uses a separate bounded
executor.  A shadow result can therefore never delay or alter the user-facing
legacy reply.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import threading
import time
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping

import redis.asyncio as async_redis
import sqlalchemy as sa

from app.config import settings
from app.core.exceptions import LLMTimeout
from app.core.time_utils import business_date, to_naive_utc, utc_now
from app.llm import get_reranker
from app.llm.base import LLMCallPolicy, LLMDeadlineExceeded, RerankResult
from app.services.recommendation_request_service import rank_candidate_dicts
from app.tasks.common import log_event

logger = logging.getLogger(__name__)

_turn_shadow_request_ids: ContextVar[set[str] | None] = ContextVar(
    "recommendation_shadow_turn_request_ids",
    default=None,
)

_PERMIT_ACQUIRE_LUA = """
local key = KEYS[1]
local now_ms = tonumber(ARGV[1])
local expires_ms = tonumber(ARGV[2])
local token = ARGV[3]
local max_permits = tonumber(ARGV[4])
redis.call('ZREMRANGEBYSCORE', key, '-inf', now_ms)
if redis.call('ZCARD', key) >= max_permits then
    return 0
end
redis.call('ZADD', key, expires_ms, token)
redis.call('PEXPIRE', key, math.max(10000, expires_ms - now_ms + 5000))
return 1
"""

_PERMIT_RELEASE_LUA = """
return redis.call('ZREM', KEYS[1], ARGV[1])
"""

_TOKEN_RESERVE_LUA = """
local key = KEYS[1]
local reserve = tonumber(ARGV[1])
local daily_limit = tonumber(ARGV[2])
local current = tonumber(redis.call('GET', key) or '0')
if current + reserve > daily_limit then
    return -1
end
local updated = redis.call('INCRBY', key, reserve)
redis.call('EXPIRE', key, 172800)
return updated
"""

_TOKEN_REFUND_LUA = """
local key = KEYS[1]
local refund = tonumber(ARGV[1])
local current = tonumber(redis.call('GET', key) or '0')
local updated = math.max(0, current - refund)
redis.call('SET', key, updated, 'EX', 172800)
return updated
"""


@dataclass(frozen=True)
class ShadowPolicy:
    queue_capacity: int = settings.recommendation_shadow_queue_capacity
    local_concurrency: int = settings.recommendation_shadow_max_concurrency
    global_concurrency: int = settings.recommendation_shadow_max_concurrency
    deadline_seconds: float = settings.recommendation_shadow_timeout_seconds
    permit_lease_seconds: float = 5.0
    persistence_threads: int = settings.recommendation_shadow_persistence_threads
    persistence_queue_capacity: int = (
        settings.recommendation_shadow_persistence_queue_capacity
    )
    max_output_tokens: int = settings.recommendation_shadow_max_output_tokens
    unactivated_state_ttl_seconds: float = 30.0


@dataclass(frozen=True)
class ShadowJob:
    request_id: str
    direction: str
    userid: str
    raw_query: str
    role: str
    criteria: Mapping[str, Any]
    query_digest: str
    candidate_dicts: tuple[Mapping[str, Any], ...]
    precision_pool_ids: tuple[str, ...]
    strategy_version: int
    algorithm_version: str
    parameters: Mapping[str, Any]
    exposure_counts: Mapping[str, int]
    recent_exposures: Mapping[str, Any]
    exposure_available: bool
    rotation_date: str
    scoring_time_utc: datetime
    deadline_monotonic: float
    submitted_monotonic: float
    provider: str
    daily_token_limit: int
    demo_id: str | None = None


@dataclass(frozen=True)
class ShadowResult:
    status: str
    queue_wait_ms: int = 0
    latency_ms: int = 0
    top_ids: tuple[str, ...] = ()
    input_tokens: int | None = None
    output_tokens: int | None = None
    fallback: str | None = None
    candidate_ids: tuple[str, ...] = ()
    precision_pool_ids: tuple[str, ...] = ()


@dataclass
class _ShadowState:
    job: ShadowJob
    active: bool = False
    discarded: bool = False
    persistence_scheduled: bool = False
    baseline_ids: tuple[str, ...] = ()
    result: ShadowResult | None = None


@dataclass(frozen=True)
class ShadowHandle:
    request_id: str


def begin_turn_tracking() -> Token:
    """Track every handle submitted while one serving turn is being built."""
    return _turn_shadow_request_ids.set(set())


def end_turn_tracking(token: Token) -> set[str]:
    """Return submitted handles and restore the caller's previous context."""
    request_ids = set(_turn_shadow_request_ids.get() or set())
    _turn_shadow_request_ids.reset(token)
    return request_ids


def _criteria_digest(criteria: Mapping[str, Any]) -> str:
    body = json.dumps(
        criteria, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _rank_delta(
    served_ids: tuple[str, ...], shadow_ids: tuple[str, ...],
) -> dict[str, int]:
    served_positions = {candidate_id: index for index, candidate_id in enumerate(served_ids, 1)}
    shadow_positions = {candidate_id: index for index, candidate_id in enumerate(shadow_ids, 1)}
    return {
        candidate_id: shadow_positions[candidate_id] - served_positions[candidate_id]
        for candidate_id in served_ids
        if candidate_id in shadow_positions
    }


def _persist_shadow_result(
    job: ShadowJob,
    result: ShadowResult,
    baseline_ids: tuple[str, ...],
) -> None:
    """Persist one activated result using an independent DB session."""
    from app.db import SessionLocal
    from app.models import RecommendationRequest, RecommendationSearchAttempt
    from app.services import demo_scope

    db = SessionLocal()
    try:
        request = db.get(RecommendationRequest, job.request_id)
        if request is None:
            log_event(
                "shadow_persistence_missing_request",
                request_id=job.request_id,
                status=result.status,
            )
            return

        next_attempt_no = int(
            db.query(
                sa.func.coalesce(sa.func.max(RecommendationSearchAttempt.attempt_no), -1),
            ).filter(
                RecommendationSearchAttempt.request_id == job.request_id,
            ).scalar()
            or 0
        ) + 1
        attempt_id = str(uuid.uuid4())
        db.add(RecommendationSearchAttempt(
            attempt_id=attempt_id,
            demo_id=job.demo_id,
            request_id=job.request_id,
            attempt_no=next_attempt_no,
            attempt_kind="shadow_candidate",
            criteria_digest=_criteria_digest(job.criteria),
            scoring_time_utc=to_naive_utc(job.scoring_time_utc),
            candidate_count=len(result.candidate_ids),
            candidate_ids=list(result.candidate_ids),
            precision_pool_ids=list(result.precision_pool_ids),
            result_count=len(result.top_ids),
            is_zero_result=not result.top_ids,
            strategy_version_id=job.strategy_version,
            algorithm_version=job.algorithm_version,
            llm_status=(
                "ok" if result.status == "completed"
                else "timeout" if result.status in ("timeout", "timeout_in_queue")
                else "skipped" if result.status == "skipped_capacity"
                else "parse_failed" if result.fallback == "LLMParseError"
                else "http_error"
            ),
            llm_input_tokens=result.input_tokens,
            llm_output_tokens=result.output_tokens,
            llm_timeout_budget_ms=max(
                0, int((job.deadline_monotonic - job.submitted_monotonic) * 1000),
            ),
            llm_retry_count=0,
            ranking_fallback=result.fallback,
            ranking_latency_ms=result.latency_ms,
            total_latency_ms=result.queue_wait_ms + result.latency_ms,
            created_at=to_naive_utc(utc_now()),
        ))
        db.flush()
        demo_scope.register(
            db, "recommendation_search_attempt", attempt_id, demo_id=job.demo_id,
        )

        request.shadow_top_ids = list(result.top_ids)
        request.shadow_overlap_count = len(set(baseline_ids) & set(result.top_ids))
        request.shadow_rank_delta = _rank_delta(baseline_ids, result.top_ids)
        request.shadow_status = result.status
        request.shadow_queue_wait_ms = result.queue_wait_ms
        request.shadow_latency_ms = result.latency_ms
        request.shadow_input_tokens = result.input_tokens
        request.shadow_output_tokens = result.output_tokens
        request.shadow_fallback = result.fallback
        db.commit()
        log_event(
            "shadow_persisted",
            request_id=job.request_id,
            direction=job.direction,
            status=result.status,
            overlap=request.shadow_overlap_count,
            queue_wait_ms=result.queue_wait_ms,
            latency_ms=result.latency_ms,
        )
    except Exception:
        db.rollback()
        logger.exception("shadow persistence failed request_id=%s", job.request_id)
        log_event(
            "shadow_persistence_failed",
            request_id=job.request_id,
            direction=job.direction,
        )
    finally:
        db.close()


class ShadowRunner:
    """Process-local bounded runner with deployment-wide Redis safeguards."""

    def __init__(
        self,
        policy: ShadowPolicy | None = None,
        *,
        reranker_factory: Callable[[], Any] = get_reranker,
        persistence_fn: Callable[
            [ShadowJob, ShadowResult, tuple[str, ...]], None
        ] = _persist_shadow_result,
        redis_factory: Callable[[], Any] | None = None,
    ):
        self.policy = policy or ShadowPolicy()
        self._reranker_factory = reranker_factory
        self._persistence_fn = persistence_fn
        self._redis_factory = redis_factory
        self._state_lock = threading.Lock()
        self._states: dict[str, _ShadowState] = {}
        self._queued_count = 0
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[ShadowJob | None] | None = None
        self._ready = threading.Event()
        self._stopping = False
        self._redis: Any = None
        self._persistence_slots = threading.BoundedSemaphore(
            self.policy.persistence_threads + self.policy.persistence_queue_capacity,
        )
        self._persistence_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.policy.persistence_threads,
            thread_name_prefix="recommendation-shadow-persist",
        )

    def _ensure_started(self) -> bool:
        with self._state_lock:
            if self._stopping:
                return False
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._thread_main,
                    name="recommendation-shadow-loop",
                    daemon=True,
                )
                self._thread.start()
        return self._ready.wait(timeout=2.0)

    def start(self) -> bool:
        """Warm the dedicated loop during process startup, before serving traffic."""
        return self._ensure_started()

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run_loop())
        finally:
            loop.run_until_complete(self._close_async_resources())
            loop.close()

    async def _run_loop(self) -> None:
        self._queue = asyncio.Queue(maxsize=self.policy.queue_capacity)
        workers = [
            asyncio.create_task(self._worker(), name=f"shadow-worker-{index}")
            for index in range(self.policy.local_concurrency)
        ]
        reaper = asyncio.create_task(
            self._reap_unactivated_states(),
            name="shadow-state-reaper",
        )
        self._ready.set()
        try:
            await asyncio.gather(*workers)
        finally:
            reaper.cancel()
            try:
                await reaper
            except asyncio.CancelledError:
                pass

    async def _reap_unactivated_states(self) -> None:
        ttl = max(0.05, float(self.policy.unactivated_state_ttl_seconds))
        interval = max(0.05, min(5.0, ttl / 2))
        while True:
            await asyncio.sleep(interval)
            cutoff = time.monotonic() - ttl
            reaped = 0
            with self._state_lock:
                stale_ids = [
                    request_id
                    for request_id, state in self._states.items()
                    if (
                        not state.active
                        and state.job.submitted_monotonic <= cutoff
                    )
                ]
                for request_id in stale_ids:
                    state = self._states.pop(request_id, None)
                    if state is not None:
                        state.discarded = True
                        reaped += 1
            if reaped:
                log_event(
                    "recommendation_shadow_unactivated_reaped",
                    reaped_count=reaped,
                )

    async def _close_async_resources(self) -> None:
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                logger.debug("shadow redis close failed", exc_info=True)
        try:
            from app.llm.providers._base import aclose_async_client

            await aclose_async_client()
        except Exception:
            logger.debug("shadow LLM client close failed", exc_info=True)

    async def _worker(self) -> None:
        assert self._queue is not None
        while True:
            job = await self._queue.get()
            if job is None:
                self._queue.task_done()
                return
            with self._state_lock:
                self._queued_count = max(0, self._queued_count - 1)
                state = self._states.get(job.request_id)
                discarded = state is None or state.discarded
            if discarded:
                self._queue.task_done()
                continue
            try:
                result = await self._execute(job)
            except Exception as exc:  # defensive: a worker must survive one bad job
                logger.exception("unhandled shadow job failure request_id=%s", job.request_id)
                result = ShadowResult(
                    status="failed",
                    queue_wait_ms=max(
                        0, int((time.monotonic() - job.submitted_monotonic) * 1000),
                    ),
                    fallback=type(exc).__name__[:32],
                    candidate_ids=tuple(str(item.get("id")) for item in job.candidate_dicts),
                    precision_pool_ids=job.precision_pool_ids,
                )
            self._complete(job.request_id, result)
            self._queue.task_done()

    async def _get_redis(self):
        if self._redis is None:
            if self._redis_factory is not None:
                self._redis = self._redis_factory()
            else:
                self._redis = async_redis.Redis.from_url(
                    settings.redis_url,
                    decode_responses=True,
                    socket_connect_timeout=settings.redis_connect_timeout_seconds,
                    socket_timeout=settings.redis_socket_timeout_seconds,
                    max_connections=settings.redis_max_connections,
                )
        return self._redis

    async def _reserve_budget(
        self, redis_client, job: ShadowJob, reserve: int,
    ) -> tuple[bool, str]:
        key = (
            f"recommendation:shadow:token_budget:{job.direction}:"
            f"{business_date(job.scoring_time_utc).isoformat()}"
        )
        try:
            result = await redis_client.eval(
                _TOKEN_RESERVE_LUA, 1, key, reserve, job.daily_token_limit,
            )
        except Exception:
            return False, "redis_unavailable"
        return (int(result) >= 0, key if int(result) >= 0 else "token_budget")

    async def _refund_budget(self, redis_client, key: str, refund: int) -> None:
        if refund <= 0:
            return
        try:
            await redis_client.eval(_TOKEN_REFUND_LUA, 1, key, refund)
        except Exception:
            logger.warning("shadow token refund failed key=%s", key, exc_info=True)

    async def _acquire_global_permit(
        self, redis_client, job: ShadowJob,
    ) -> tuple[str | None, str]:
        token = str(uuid.uuid4())
        key = f"recommendation:shadow:permits:{job.provider}"
        now_ms = int(time.time() * 1000)
        expires_ms = now_ms + int(self.policy.permit_lease_seconds * 1000)
        try:
            acquired = await redis_client.eval(
                _PERMIT_ACQUIRE_LUA,
                1,
                key,
                now_ms,
                expires_ms,
                token,
                self.policy.global_concurrency,
            )
        except Exception:
            return None, "redis_unavailable"
        return (token, key) if int(acquired) == 1 else (None, "global_capacity")

    async def _release_global_permit(
        self, redis_client, key: str, token: str,
    ) -> None:
        try:
            await redis_client.eval(_PERMIT_RELEASE_LUA, 1, key, token)
        except Exception:
            logger.warning("shadow global permit release failed key=%s", key, exc_info=True)

    @staticmethod
    def _token_reserve(job: ShadowJob, max_output_tokens: int) -> int:
        body = json.dumps(
            {
                "query": job.raw_query,
                "candidates": list(job.candidate_dicts),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        # UTF-8 byte length is deliberately conservative for Chinese text
        # (often close to one token per character) and for ASCII/JSON syntax.
        # Dividing Python's character count by four severely under-reserves
        # Chinese prompts and can let a deployment exceed its daily budget.
        pessimistic_input = max(1, len(body.encode("utf-8")))
        return pessimistic_input + max(1, int(max_output_tokens))

    async def _execute(self, job: ShadowJob) -> ShadowResult:
        started = time.monotonic()
        queue_wait_ms = max(
            0, int((started - job.submitted_monotonic) * 1000),
        )
        remaining = job.deadline_monotonic - started
        candidate_ids = tuple(str(item.get("id")) for item in job.candidate_dicts)
        if remaining <= 0:
            return ShadowResult(
                status="timeout_in_queue",
                queue_wait_ms=queue_wait_ms,
                fallback="deadline_in_queue",
                candidate_ids=candidate_ids,
                precision_pool_ids=job.precision_pool_ids,
            )
        if job.daily_token_limit <= 0:
            return ShadowResult(
                status="skipped_capacity",
                queue_wait_ms=queue_wait_ms,
                fallback="token_budget",
                candidate_ids=candidate_ids,
                precision_pool_ids=job.precision_pool_ids,
            )

        redis_client = await self._get_redis()
        reserve = self._token_reserve(job, self.policy.max_output_tokens)
        pessimistic_output_tokens = max(1, int(self.policy.max_output_tokens))
        pessimistic_input_tokens = max(1, reserve - pessimistic_output_tokens)
        budget_ok, budget_key_or_reason = await self._reserve_budget(
            redis_client, job, reserve,
        )
        if not budget_ok:
            return ShadowResult(
                status="skipped_capacity",
                queue_wait_ms=queue_wait_ms,
                fallback=budget_key_or_reason,
                candidate_ids=candidate_ids,
                precision_pool_ids=job.precision_pool_ids,
            )
        budget_key = budget_key_or_reason

        permit_token, permit_key_or_reason = await self._acquire_global_permit(
            redis_client, job,
        )
        if permit_token is None:
            await self._refund_budget(redis_client, budget_key, reserve)
            return ShadowResult(
                status="skipped_capacity",
                queue_wait_ms=queue_wait_ms,
                fallback=permit_key_or_reason,
                candidate_ids=candidate_ids,
                precision_pool_ids=job.precision_pool_ids,
            )
        permit_key = permit_key_or_reason

        llm_result: RerankResult | None = None
        try:
            candidates_by_id = {
                str(item.get("id")): item for item in job.candidate_dicts
            }
            precision_candidates = [
                dict(candidates_by_id[candidate_id])
                for candidate_id in job.precision_pool_ids
                if candidate_id in candidates_by_id
            ]
            reranker = self._reranker_factory()
            call_policy = LLMCallPolicy(
                deadline_monotonic=job.deadline_monotonic,
                max_retries=0,
            )
            llm_result = await reranker.arerank(
                query=job.raw_query,
                candidates=precision_candidates,
                role=job.role,
                top_n=3,
                call_policy=call_policy,
            )
            ordered, _items = rank_candidate_dicts(
                list(job.candidate_dicts),
                direction=job.direction,
                criteria=job.criteria,
                userid=job.userid,
                query_digest=job.query_digest,
                strategy_version=job.strategy_version,
                parameters=job.parameters,
                semantic_ranked_items=llm_result.ranked_items,
                exposure_counts=job.exposure_counts,
                recent_exposures=job.recent_exposures,
                exposure_available=job.exposure_available,
                precision_pool_ids=list(job.precision_pool_ids),
                rotation_date=job.rotation_date,
                now=job.scoring_time_utc,
            )
            actual_usage = None
            if llm_result.input_tokens is not None and llm_result.output_tokens is not None:
                actual_usage = max(
                    0, int(llm_result.input_tokens) + int(llm_result.output_tokens),
                )
            if actual_usage is not None and actual_usage < reserve:
                await self._refund_budget(redis_client, budget_key, reserve - actual_usage)
            # Provider 未返回 usage 时，Redis 预算已按上界预占且不会退款；持久事实
            # 也必须保存同一悲观口径，避免报表把实际发生过的调用记成 0 token。
            reported_input_tokens = (
                llm_result.input_tokens
                if actual_usage is not None else pessimistic_input_tokens
            )
            reported_output_tokens = (
                llm_result.output_tokens
                if actual_usage is not None else pessimistic_output_tokens
            )
            return ShadowResult(
                status="completed",
                queue_wait_ms=queue_wait_ms,
                latency_ms=max(0, int((time.monotonic() - started) * 1000)),
                top_ids=tuple(item.candidate_id for item in ordered[:3]),
                input_tokens=reported_input_tokens,
                output_tokens=reported_output_tokens,
                candidate_ids=candidate_ids,
                precision_pool_ids=job.precision_pool_ids,
            )
        except (LLMDeadlineExceeded, LLMTimeout, asyncio.TimeoutError, TimeoutError):
            known_input_tokens = getattr(llm_result, "input_tokens", None)
            known_output_tokens = getattr(llm_result, "output_tokens", None)
            return ShadowResult(
                status="timeout",
                queue_wait_ms=queue_wait_ms,
                latency_ms=max(0, int((time.monotonic() - started) * 1000)),
                input_tokens=(
                    known_input_tokens
                    if known_input_tokens is not None else pessimistic_input_tokens
                ),
                output_tokens=(
                    known_output_tokens
                    if known_output_tokens is not None else pessimistic_output_tokens
                ),
                fallback="deadline",
                candidate_ids=candidate_ids,
                precision_pool_ids=job.precision_pool_ids,
            )
        except Exception as exc:
            logger.warning(
                "shadow provider/ranking failed request_id=%s: %s",
                job.request_id,
                exc,
                exc_info=True,
            )
            known_input_tokens = getattr(llm_result, "input_tokens", None)
            known_output_tokens = getattr(llm_result, "output_tokens", None)
            return ShadowResult(
                status="failed",
                queue_wait_ms=queue_wait_ms,
                latency_ms=max(0, int((time.monotonic() - started) * 1000)),
                input_tokens=(
                    known_input_tokens
                    if known_input_tokens is not None else pessimistic_input_tokens
                ),
                output_tokens=(
                    known_output_tokens
                    if known_output_tokens is not None else pessimistic_output_tokens
                ),
                fallback=type(exc).__name__[:32],
                candidate_ids=candidate_ids,
                precision_pool_ids=job.precision_pool_ids,
            )
        finally:
            await self._release_global_permit(redis_client, permit_key, permit_token)

    def submit(self, job: ShadowJob) -> ShadowHandle | None:
        """Submit without blocking the serving thread; reject a full local queue."""
        if not self._ensure_started() or self._loop is None or self._queue is None:
            return None
        with self._state_lock:
            if self._stopping or job.request_id in self._states:
                return None
            state = _ShadowState(job=job)
            self._states[job.request_id] = state
            tracked = _turn_shadow_request_ids.get()
            if tracked is not None:
                tracked.add(job.request_id)
            if self._queued_count >= self.policy.queue_capacity:
                state.result = ShadowResult(
                    status="skipped_capacity",
                    fallback="local_capacity",
                    candidate_ids=tuple(
                        str(item.get("id")) for item in job.candidate_dicts
                    ),
                    precision_pool_ids=job.precision_pool_ids,
                )
                return ShadowHandle(job.request_id)
            self._queued_count += 1
        self._loop.call_soon_threadsafe(self._queue.put_nowait, job)
        return ShadowHandle(job.request_id)

    def set_served_baseline(
        self, request_id: str, served_top_ids: list[str] | tuple[str, ...],
    ) -> None:
        with self._state_lock:
            state = self._states.get(request_id)
            if state is None or state.discarded:
                return
            state.baseline_ids = tuple(str(item) for item in served_top_ids)

    def activate_persistence(self, request_id: str) -> None:
        """Allow persistence only after the served transaction committed."""
        with self._state_lock:
            state = self._states.get(request_id)
            if state is None or state.discarded:
                return
            state.active = True
        self._maybe_schedule_persistence(request_id)

    def discard(self, request_id: str) -> None:
        """Suppress persistence when the served transaction rolled back."""
        with self._state_lock:
            state = self._states.pop(request_id, None)
            if state is not None:
                state.discarded = True

    def _complete(self, request_id: str, result: ShadowResult) -> None:
        with self._state_lock:
            state = self._states.get(request_id)
            if state is None or state.discarded:
                return
            state.result = result
        log_event(
            "recommendation_shadow_result",
            request_id=request_id,
            status=result.status,
            queue_wait_ms=result.queue_wait_ms,
            latency_ms=result.latency_ms,
            fallback=result.fallback,
        )
        self._maybe_schedule_persistence(request_id)

    def _maybe_schedule_persistence(self, request_id: str) -> None:
        with self._state_lock:
            state = self._states.get(request_id)
            if (
                state is None
                or state.discarded
                or not state.active
                or state.result is None
                or state.persistence_scheduled
            ):
                return
            if not self._persistence_slots.acquire(blocking=False):
                state.persistence_scheduled = True
                self._states.pop(request_id, None)
                log_event(
                    "shadow_persistence_dropped",
                    request_id=request_id,
                    direction=state.job.direction,
                )
                return
            state.persistence_scheduled = True
            job = state.job
            result = state.result
            baseline_ids = state.baseline_ids

        future = self._persistence_executor.submit(
            self._persistence_fn, job, result, baseline_ids,
        )

        def _done(_future) -> None:
            self._persistence_slots.release()
            with self._state_lock:
                self._states.pop(request_id, None)
            try:
                _future.result()
            except Exception:
                logger.exception(
                    "shadow persistence executor failed request_id=%s", request_id,
                )

        future.add_done_callback(_done)

    def shutdown(self, timeout: float = 5.0) -> None:
        with self._state_lock:
            if self._stopping:
                return
            self._stopping = True
            loop, queue, thread = self._loop, self._queue, self._thread
        if loop is not None and queue is not None:
            async def _enqueue_stops() -> None:
                for _ in range(self.policy.local_concurrency):
                    await queue.put(None)

            stop_future = asyncio.run_coroutine_threadsafe(_enqueue_stops(), loop)
            try:
                stop_future.result(timeout=max(0.1, timeout))
            except Exception:
                logger.warning("shadow runner stop enqueue timed out", exc_info=True)
        if thread is not None:
            thread.join(timeout=max(0.1, timeout))
        self._persistence_executor.shutdown(wait=False, cancel_futures=True)


shadow_runner = ShadowRunner()


def start_shadow_runner() -> bool:
    return shadow_runner.start()


def activate_persistence(request_id: str) -> None:
    shadow_runner.activate_persistence(request_id)


def discard(request_id: str) -> None:
    shadow_runner.discard(request_id)


def set_served_baseline(
    request_id: str, served_top_ids: list[str] | tuple[str, ...],
) -> None:
    shadow_runner.set_served_baseline(request_id, served_top_ids)


def shutdown_shadow_runner(timeout: float = 5.0) -> None:
    shadow_runner.shutdown(timeout=timeout)


__all__ = [
    "ShadowHandle",
    "ShadowJob",
    "ShadowPolicy",
    "ShadowResult",
    "ShadowRunner",
    "activate_persistence",
    "begin_turn_tracking",
    "discard",
    "end_turn_tracking",
    "set_served_baseline",
    "shadow_runner",
    "start_shadow_runner",
    "shutdown_shadow_runner",
]
