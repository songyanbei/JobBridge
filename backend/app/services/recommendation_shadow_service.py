"""Bounded, fail-closed shadow execution.

Shadow work is deliberately detached from the serving transaction: it can
observe candidate ranking without ever changing the ReplyMessage or session.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from app.services.recommendation_request_service import rank_candidate_dicts


@dataclass(frozen=True)
class ShadowPolicy:
    queue_size: int = 128
    global_permits: int = 8
    deadline_seconds: float = 3.0
    token_budget: int = 1200


class ShadowRunner:
    def __init__(self, policy: ShadowPolicy | None = None):
        self.policy = policy or ShadowPolicy()
        self._queue = asyncio.Queue(maxsize=self.policy.queue_size)
        self._permits = asyncio.Semaphore(self.policy.global_permits)
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.policy.global_permits,
            thread_name_prefix="recommendation-shadow",
        )

    async def run(
        self,
        *,
        candidate_dicts: list[dict],
        direction: str,
        criteria: dict,
        userid: str,
        query_digest: str,
        strategy_version: int,
        parameters: dict,
    ) -> dict:
        if len(candidate_dicts) > 50:
            candidate_dicts = candidate_dicts[:50]
        started = time.perf_counter()
        async with self._permits:
            loop = asyncio.get_running_loop()
            try:
                # rank_candidate_dicts has keyword-only arguments, so invoke
                # through a closure rather than relying on executor kwargs.
                task = loop.run_in_executor(
                    self._executor,
                    lambda: rank_candidate_dicts(
                        candidate_dicts,
                        direction=direction,
                        criteria=criteria,
                        userid=userid,
                        query_digest=query_digest,
                        strategy_version=strategy_version,
                        parameters=parameters,
                    ),
                )
                ranked, items = await asyncio.wait_for(
                    task, timeout=self.policy.deadline_seconds,
                )
                return {
                    "status": "completed",
                    "attempt_id": str(uuid.uuid4()),
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                    "ranked_ids": [item.candidate_id for item in ranked],
                    "items": items,
                    "token_budget": self.policy.token_budget,
                }
            except asyncio.TimeoutError:
                return {
                    "status": "deadline_exceeded",
                    "attempt_id": str(uuid.uuid4()),
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                    "ranked_ids": [],
                    "items": [],
                }

    def submit(self, **kwargs):
        """Best-effort detached submission for synchronous request handlers."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        return loop.create_task(self.run(**kwargs))


shadow_runner = ShadowRunner()
