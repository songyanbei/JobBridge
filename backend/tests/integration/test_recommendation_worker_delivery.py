"""Delivery lifecycle facts are durable in real MySQL."""
from __future__ import annotations

from uuid import uuid4

import pytest

from app.db import SessionLocal
from app.models import RecommendationRequest, RecommendationSearchAttempt
from app.services.recommendation_delivery_service import _persist_request_facts
from .recommendation_integration_support import naive_utc_now

pytestmark = pytest.mark.integration


def test_request_and_served_attempt_commit_as_one_lineage(unique_prefix):
    request_id = str(uuid4())
    db = SessionLocal()
    try:
        fact = {
            "request_id": request_id,
            "request_kind": "initial_search",
            "viewer_userid": f"{unique_prefix}-viewer",
            "direction": "search_job",
            "query_digest": "integration",
            "algorithm_version": "legacy",
            "candidate_ids": ["1", "2"],
            "served_top_ids": ["1"],
            "candidate_count": 2,
            "result_count": 1,
            "execution_mode": "off",
            "served_assignment": "legacy",
        }
        _persist_request_facts(
            db,
            request_id=request_id,
            source_inbound_msg_id=f"{unique_prefix}-msg",
            request_index=0,
            userid=f"{unique_prefix}-viewer",
            snapshot_id=None,
            ctx={},
            fact=fact,
            items=[{"target_id": 1}],
            now=naive_utc_now(),
        )
        db.commit()
        row = db.get(RecommendationRequest, request_id)
        assert row is not None
        assert row.served_attempt_id is not None
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
