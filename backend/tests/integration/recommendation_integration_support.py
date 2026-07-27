"""Small builders shared by recommendation integration modules.

Rows use UUID-based natural keys so tests can run repeatedly against a developer
database without colliding. Each test remains responsible for deleting rows it
commits.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import (
    RecommendationDelivery,
    RecommendationRequest,
    RecommendationSearchAttempt,
    User,
)


def naive_utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def user(userid: str, *, role: str = "worker") -> User:
    return User(
        external_userid=userid,
        role=role,
        status="active",
        display_name="integration user",
        can_search_jobs=role in {"worker", "broker"},
        can_search_workers=role in {"factory", "broker"},
    )


def request(request_id: str, source_id: str, userid: str, **overrides):
    values = {
        "request_id": request_id,
        "source_inbound_msg_id": source_id,
        "request_index": 0,
        "request_kind": "initial_search",
        "served_attempt_id": None,
        "viewer_userid": userid,
        "direction": "search_job",
        "query_digest": "0123456789abcdef",
        "execution_mode": "off",
        "served_assignment": "legacy",
        "algorithm_version": "legacy",
        "final_candidate_count": 0,
        "result_count": 0,
        "is_zero_result": True,
        "show_more_exhausted": False,
        "total_latency_ms": 0,
        "served_top_ids": [],
        "served_owner_count": 0,
        "served_max_owner_items": 0,
        "served_exploration_count": 0,
        "created_at": naive_utc_now(),
    }
    values.update(overrides)
    return RecommendationRequest(**values)


def attempt(attempt_id: str, request_id: str, **overrides):
    values = {
        "attempt_id": attempt_id,
        "request_id": request_id,
        "attempt_no": 0,
        "attempt_kind": "initial",
        "criteria_digest": "a" * 64,
        "scoring_time_utc": naive_utc_now(),
        "candidate_count": 0,
        "candidate_ids": [],
        "precision_pool_ids": [],
        "result_count": 0,
        "is_zero_result": True,
        "algorithm_version": "legacy",
        "llm_status": "skipped",
        "llm_retry_count": 0,
        "ranking_latency_ms": 0,
        "total_latency_ms": 0,
        "created_at": naive_utc_now(),
    }
    values.update(overrides)
    return RecommendationSearchAttempt(**values)


def delivery(delivery_id: str, source_id: str, request_id: str, userid: str, **overrides):
    now = naive_utc_now()
    values = {
        "delivery_id": delivery_id,
        "source_inbound_msg_id": source_id,
        "reply_index": 0,
        "request_id": request_id,
        "userid": userid,
        "recommendation_context": {},
        "session_commit_token": delivery_id,
        "status": "pending",
        "attempt_count": 0,
        "next_attempt_at": now,
        "impression_state": "pending",
        "impression_attempt_count": 0,
        "impression_next_attempt_at": now,
        "content_expires_at": now + timedelta(hours=24),
        "created_at": now,
        "updated_at": now,
    }
    values.update(overrides)
    return RecommendationDelivery(**values)
