"""Real MySQL lock ordering for media and target cleanup pipelines."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading
from uuid import uuid4

import pytest
from sqlalchemy import event

from app.db import SessionLocal
from app.models import (
    Job,
    MediaAssetLifecycle,
    RecommendationDelivery,
    RecommendationRequest,
    TargetCleanupTask,
    User,
    WecomInboundEvent,
    WecomOutboundOutbox,
)
from app.services import recommendation_privacy_service as privacy
from app.services import target_cleanup_service
from app.tasks import media_cleanup_worker

from .recommendation_integration_support import delivery, request, user


pytestmark = pytest.mark.integration


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _deleted_job(userid: str, now: datetime) -> Job:
    return Job(
        owner_userid=userid,
        city="lock-order",
        job_category="lock-order",
        salary_floor_monthly=5000,
        pay_type="月薪",
        headcount=1,
        raw_text="media target cleanup lock order",
        audit_status="passed",
        activated_at=now - timedelta(days=10),
        expires_at=now - timedelta(days=1),
        deleted_at=now - timedelta(hours=1),
        delist_reason="expired",
        version=2,
    )


def _cleanup_rows(db, *, userid: str, job_id: int, delivery_id: str | None = None):
    if delivery_id is not None:
        db.query(WecomOutboundOutbox).filter(
            WecomOutboundOutbox.recommendation_delivery_id == delivery_id,
        ).delete(synchronize_session=False)
        db.query(RecommendationDelivery).filter(
            RecommendationDelivery.delivery_id == delivery_id,
        ).delete(synchronize_session=False)
        db.query(RecommendationRequest).filter(
            RecommendationRequest.viewer_userid == userid,
        ).delete(synchronize_session=False)
        db.query(WecomInboundEvent).filter(
            WecomInboundEvent.from_userid == userid,
        ).delete(synchronize_session=False)
    db.query(TargetCleanupTask).filter(
        TargetCleanupTask.target_type == "job",
        TargetCleanupTask.target_id == job_id,
    ).delete(synchronize_session=False)
    db.query(MediaAssetLifecycle).filter(
        MediaAssetLifecycle.entity_type == "job",
        MediaAssetLifecycle.entity_id == job_id,
    ).delete(synchronize_session=False)
    db.query(Job).filter(Job.id == job_id).delete(synchronize_session=False)
    db.query(User).filter(User.external_userid == userid).delete(
        synchronize_session=False,
    )
    db.commit()


def test_user_cleanup_and_target_worker_follow_one_lock_order(monkeypatch):
    prefix = uuid4().hex[:12]
    userid = f"cleanup-order-{prefix}"
    source_id = f"cleanup-order-source-{prefix}"
    request_id = str(uuid4())
    delivery_id = str(uuid4())
    target_owner = f"target-owner-{prefix}"
    setup_db = SessionLocal()
    engine = setup_db.get_bind()
    target_reached_outbox = threading.Event()
    privacy_reached_target = threading.Event()
    allow_target_to_continue = threading.Event()
    target_errors = []
    privacy_errors = []
    target_result = []
    target_thread = None
    privacy_thread = None
    job_id = 0
    media_id = 0
    task_id = 0
    observed = {"target": [], "privacy": []}

    def _record_and_coordinate(
        _conn, _cursor, statement, _params, _context, _many,
    ):
        normalized = " ".join(statement.lower().split())
        if "for update" not in normalized:
            return
        current = threading.current_thread()
        if current is target_thread:
            if "from target_cleanup_task" in normalized:
                observed["target"].append("target")
            elif "from wecom_outbound_outbox" in normalized:
                observed["target"].append("outbox")
                target_reached_outbox.set()
                assert allow_target_to_continue.wait(timeout=5)
            elif "from recommendation_delivery" in normalized:
                observed["target"].append("delivery")
        elif current is privacy_thread:
            if "from job" in normalized:
                observed["privacy"].append("job")
            elif "from media_asset_lifecycle" in normalized:
                observed["privacy"].append("media")
            elif "from target_cleanup_task" in normalized:
                observed["privacy"].append("target")
                privacy_reached_target.set()
            elif "from wecom_outbound_outbox" in normalized:
                observed["privacy"].append("outbox")
            elif "from recommendation_delivery" in normalized:
                observed["privacy"].append("delivery")

    try:
        now = _now()
        setup_db.add(user(userid, role="factory"))
        setup_db.flush()
        job = _deleted_job(userid, now)
        setup_db.add(job)
        inbound = WecomInboundEvent(
            msg_id=source_id,
            from_userid=userid,
            msg_type="text",
            content_brief="cleanup lock order",
            status="done",
        )
        setup_db.add(inbound)
        setup_db.add(request(request_id, source_id, userid))
        setup_db.flush()
        job_id = int(job.id)
        setup_db.add(MediaAssetLifecycle(
            object_key=f"images/{prefix}.jpg",
            owner_userid=userid,
            entity_type="job",
            entity_id=job_id,
            state="attached",
        ))
        recommendation = delivery(
            delivery_id,
            source_id,
            request_id,
            userid,
            status="pending",
            content_ciphertext=b"encrypted-content",
            session_patch_ciphertext=b"encrypted-patch",
            recommendation_context={
                "direction": "search_job",
                "served_top_ids": [str(job_id)],
                "items": [{
                    "target_type": "job",
                    "target_id": job_id,
                    "position": 1,
                }],
            },
        )
        setup_db.add(recommendation)
        setup_db.flush()
        setup_db.add(WecomOutboundOutbox(
            inbound_event_id=int(inbound.id),
            reply_index=0,
            userid=userid,
            msg_type="text",
            recommendation_delivery_id=delivery_id,
            status="pending",
        ))
        task = TargetCleanupTask(
            operation_id=str(uuid4()),
            target_type="job",
            target_id=job_id,
            reason="expired",
            reason_history=["expired"],
            status="processing",
            attempt_count=1,
            lease_owner=target_owner,
            lease_expires_at=now + timedelta(minutes=4),
        )
        setup_db.add(task)
        setup_db.commit()
        media_id = int(setup_db.query(MediaAssetLifecycle.id).filter(
            MediaAssetLifecycle.entity_id == job_id,
        ).scalar())
        task_id = int(task.id)

        monkeypatch.setattr(
            privacy, "scrub_recommendation_sessions", lambda *_args, **_kwargs: 0,
        )

        def _run_target():
            with SessionLocal() as db:
                try:
                    target_result.append(
                        target_cleanup_service.process_cleanup_task(
                            db, task_id, target_owner,
                        )
                    )
                except Exception as exc:
                    target_errors.append(exc)

        def _run_privacy():
            with SessionLocal() as db:
                try:
                    report = privacy.delete_recommendation_user_data(
                        db,
                        userid,
                        now=_now(),
                        commit=False,
                        batch_id=f"batch-{prefix}",
                    )
                    assert report.ok, report.failed_steps
                    db.commit()
                except Exception as exc:
                    db.rollback()
                    privacy_errors.append(exc)

        event.listen(engine, "before_cursor_execute", _record_and_coordinate)
        target_thread = threading.Thread(target=_run_target)
        target_thread.start()
        assert target_reached_outbox.wait(timeout=5)

        privacy_thread = threading.Thread(target=_run_privacy)
        privacy_thread.start()
        assert privacy_reached_target.wait(timeout=5)
        allow_target_to_continue.set()

        target_thread.join(timeout=10)
        privacy_thread.join(timeout=10)
        event.remove(engine, "before_cursor_execute", _record_and_coordinate)

        assert not target_thread.is_alive()
        assert not privacy_thread.is_alive()
        assert target_errors == []
        assert privacy_errors == []
        assert target_result == [True]
        assert observed["target"][:3] == ["target", "outbox", "delivery"]
        privacy_order = observed["privacy"]
        assert privacy_order.index("job") < privacy_order.index("media")
        assert privacy_order.index("media") < privacy_order.index("target")
        assert privacy_order.index("target") < privacy_order.index("outbox")
        assert privacy_order.index("outbox") < privacy_order.index("delivery")

        with SessionLocal() as verify_db:
            saved_media = verify_db.get(MediaAssetLifecycle, media_id)
            saved_task = verify_db.get(TargetCleanupTask, task_id)
            assert saved_media.state == "delete_pending"
            assert saved_task.status == "succeeded"
    finally:
        allow_target_to_continue.set()
        if event.contains(engine, "before_cursor_execute", _record_and_coordinate):
            event.remove(engine, "before_cursor_execute", _record_and_coordinate)
        if target_thread is not None:
            target_thread.join(timeout=10)
        if privacy_thread is not None:
            privacy_thread.join(timeout=10)
        setup_db.rollback()
        if job_id:
            _cleanup_rows(
                setup_db,
                userid=userid,
                job_id=job_id,
                delivery_id=delivery_id,
            )
        setup_db.close()


def test_failed_media_and_target_tasks_remain_retryable():
    prefix = uuid4().hex[:12]
    userid = f"cleanup-retry-{prefix}"
    db = SessionLocal()
    job_id = 0
    media_id = 0
    task_id = 0
    try:
        now = _now()
        db.add(user(userid, role="factory"))
        db.flush()
        job = _deleted_job(userid, now)
        db.add(job)
        db.flush()
        job_id = int(job.id)
        db.add(MediaAssetLifecycle(
            object_key=f"images/{prefix}.jpg",
            owner_userid=userid,
            entity_type="job",
            entity_id=job_id,
            state="attached",
        ))
        db.commit()

        report = privacy.PrivacyReport(batch_id=f"batch-{prefix}")
        privacy._delete_owned_content(
            db,
            userid,
            [privacy.TargetRef("job", job_id)],
            report,
            now=now,
            commit=False,
        )
        db.commit()
        media = db.query(MediaAssetLifecycle).filter(
            MediaAssetLifecycle.entity_id == job_id,
        ).one()
        task = db.query(TargetCleanupTask).filter(
            TargetCleanupTask.target_id == job_id,
        ).one()
        media_id = int(media.id)
        task_id = int(task.id)
        assert media.state == "delete_pending"
        assert task.status == "pending"

        claim_at = now + timedelta(seconds=1)
        media.lease_owner = "media-failure-owner"
        media.lease_expires_at = claim_at + timedelta(minutes=2)
        task.status = "processing"
        task.attempt_count = 1
        task.lease_owner = "target-failure-owner"
        task.lease_expires_at = claim_at + timedelta(minutes=4)
        db.commit()

        assert media_cleanup_worker._finish_claimed_result(
            db,
            media_id,
            "media-failure-owner",
            error=RuntimeError("storage unavailable"),
            now=claim_at + timedelta(seconds=1),
        ) == "retry_wait"

        assert target_cleanup_service.fail_cleanup_task(
            db,
            task_id,
            "target-failure-owner",
            RuntimeError("redaction unavailable"),
            claim_at + timedelta(seconds=1),
        )

        db.expire_all()
        failed_media = db.get(MediaAssetLifecycle, media_id)
        failed_target = db.get(TargetCleanupTask, task_id)
        assert failed_media.state == "delete_pending"
        assert failed_media.attempt_count == 1
        assert failed_media.next_attempt_at is not None
        assert failed_media.lease_owner is None
        assert failed_target.status == "retry_wait"
        assert failed_target.attempt_count == 1
        assert failed_target.next_attempt_at is not None
        assert failed_target.lease_owner is None
    finally:
        db.rollback()
        if job_id:
            _cleanup_rows(db, userid=userid, job_id=job_id)
        db.close()
