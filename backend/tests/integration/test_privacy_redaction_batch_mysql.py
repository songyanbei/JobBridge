"""MySQL coverage for a redaction candidate page shrinking under concurrency."""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.db import SessionLocal
from app.models import RecommendationDelivery, RecommendationRequest, User
from app.services import recommendation_privacy_service as privacy

from .recommendation_integration_support import (
    delivery,
    naive_utc_now,
    request,
    user,
)


pytestmark = pytest.mark.integration


def test_full_candidate_page_continues_after_concurrent_redaction(monkeypatch):
    prefix = uuid4().hex[:12]
    userid = f"privacy-batch-{prefix}"
    request_id = f"req-{prefix}"
    delivery_ids = [f"{prefix}-{index:04d}" for index in range(501)]
    candidate_ready = threading.Event()
    concurrent_done = threading.Event()
    concurrent_errors = []
    first_candidate = []
    candidate_page_sizes = []
    original_lock = privacy._lock_redactable_deliveries

    def _pause_before_first_lock(
        db, external_userid, candidate_ids, pending_filter,
    ):
        candidate_page_sizes.append(len(candidate_ids))
        if len(candidate_page_sizes) == 1:
            first_candidate.append(candidate_ids[0])
            candidate_ready.set()
            assert concurrent_done.wait(timeout=10)
        return original_lock(
            db, external_userid, candidate_ids, pending_filter,
        )

    def _concurrent_redaction():
        try:
            assert candidate_ready.wait(timeout=10)
            with SessionLocal() as concurrent_db:
                concurrent_db.query(RecommendationDelivery).filter(
                    RecommendationDelivery.delivery_id == first_candidate[0],
                ).update(
                    {
                        "content_ciphertext": None,
                        "session_patch_ciphertext": None,
                        "content_expires_at": naive_utc_now(),
                    },
                    synchronize_session=False,
                )
                concurrent_db.commit()
        except Exception as exc:
            concurrent_errors.append(exc)
        finally:
            concurrent_done.set()

    monkeypatch.setattr(
        privacy, "_lock_redactable_deliveries", _pause_before_first_lock,
    )

    setup_db = SessionLocal()
    competitor = None
    try:
        setup_db.add(user(userid))
        setup_db.add(request(
            request_id,
            f"request-source-{prefix}",
            userid,
        ))
        setup_db.flush()
        rows = [
            delivery(
                delivery_id,
                f"source-{prefix}-{index:04d}",
                request_id,
                userid,
                status="sent",
                content_ciphertext=b"encrypted-content",
                session_patch_ciphertext=b"encrypted-session-patch",
                content_expires_at=naive_utc_now(),
                impression_state="completed",
                sent_at=naive_utc_now(),
            )
            for index, delivery_id in enumerate(delivery_ids)
        ]
        setup_db.add_all(rows)
        setup_db.commit()

        competitor = threading.Thread(target=_concurrent_redaction)
        competitor.start()
        with SessionLocal() as worker_db:
            changed = privacy.redact_user_recommendation_content(
                worker_db,
                userid,
                now=datetime.now(timezone.utc),
                commit=False,
            )
            worker_db.commit()
        competitor.join(timeout=10)

        assert not competitor.is_alive()
        assert concurrent_errors == []
        assert candidate_page_sizes == [500, 1]
        assert changed == 500
        with SessionLocal() as verify_db:
            remaining = verify_db.query(RecommendationDelivery).filter(
                RecommendationDelivery.userid == userid,
                (
                    RecommendationDelivery.content_ciphertext.isnot(None)
                    | RecommendationDelivery.session_patch_ciphertext.isnot(None)
                ),
            ).count()
            tail = verify_db.get(RecommendationDelivery, delivery_ids[-1])
            assert remaining == 0
            assert tail.content_ciphertext is None
            assert tail.session_patch_ciphertext is None
    finally:
        concurrent_done.set()
        if competitor is not None:
            competitor.join(timeout=10)
        setup_db.rollback()
        setup_db.query(RecommendationDelivery).filter(
            RecommendationDelivery.userid == userid,
        ).delete(synchronize_session=False)
        setup_db.query(RecommendationRequest).filter(
            RecommendationRequest.request_id == request_id,
        ).delete(synchronize_session=False)
        setup_db.query(User).filter(
            User.external_userid == userid,
        ).delete(synchronize_session=False)
        setup_db.commit()
        setup_db.close()
