"""Minimal real-service gates for Phase 11 stage 5.

These tests deliberately cover only the two cross-process boundaries which
unit tests cannot prove: the Resume row lock around late recommendation facts,
and the Redis revocation fence used by target cleanup.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event, current_thread
from uuid import uuid4

import pytest
from sqlalchemy import event

from app.core.redis_client import (
    RECOMMENDATION_SESSION_REVOCATION_FENCE_KEY,
    RECOMMENDATION_SESSION_TARGET_INDEX_PREFIX,
    get_redis,
)
from app.db import SessionLocal, engine
from app.models import (
    RecommendationDelivery,
    RecommendationRequest,
    RecommendationSearchAttempt,
    Resume,
    TargetCleanupTask,
    User,
    WecomInboundEvent,
    WecomOutboundOutbox,
)
from app.services.recommendation_delivery_service import (
    RecommendationTargetStale,
    persist_request_fact_only,
)
from app.services.target_cleanup_service import process_cleanup_task
from app.tasks.resume_candidate_cleanup import cleanup_candidate
from app.tasks.resume_expiry_cleanup import expire_locked_batch


pytestmark = pytest.mark.integration


def test_expiry_cleanup_dispatcher_lock_order_has_no_three_session_cycle(monkeypatch):
    """The minimal former cycle converges without a deadlock or a send.

    Event barriers create the exact three-session overlap without timing sleeps:
    cleanup owns TargetCleanupTask, expiry owns Resume, and dispatcher reaches
    its Resume lock before either holder is released.  Dispatcher must not own
    outbox at that point; cleanup can therefore finish, then expiry, then claim.
    """
    from app.services import recommendation_privacy_service as privacy_service
    from app.tasks import resume_expiry_cleanup as expiry_service
    from app.services.worker import Worker

    token = uuid4().hex
    owner = f"stage5-lock-{token[:16]}"
    source_id = f"stage5-lock-{token}"
    delivery_id = str(uuid4())
    request_id = str(uuid4())
    worker_owner = f"cleanup-{token[:16]}"
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    ids: dict[str, int] = {}

    with SessionLocal() as db:
        db.add(User(external_userid=owner, role="worker"))
        db.flush()
        resume = Resume(
            owner_userid=owner,
            expected_cities=["苏州"],
            expected_job_categories=["普工"],
            salary_expect_floor_monthly=5000,
            gender="男",
            age=30,
            accept_long_term=True,
            accept_short_term=False,
            raw_text="stage5 three session lock order",
            audit_status="passed",
            activated_at=now - timedelta(days=31),
            expires_at=now - timedelta(seconds=1),
            candidate_expires_at=None,
        )
        db.add(resume)
        db.flush()
        ids["resume"] = int(resume.id)
        inbound = WecomInboundEvent(
            msg_id=source_id,
            from_userid=owner,
            msg_type="text",
            content_brief="stage5 lock order",
            status="done",
        )
        db.add(inbound)
        db.flush()
        ids["inbound"] = int(inbound.id)
        db.add(RecommendationRequest(
            request_id=request_id,
            source_inbound_msg_id=source_id,
            request_index=0,
            request_kind="initial_search",
            viewer_userid=owner,
            direction="search_worker",
            query_digest="stage5lockorder",
            execution_mode="off",
            served_assignment="legacy",
            algorithm_version="legacy",
            final_candidate_count=1,
            result_count=1,
            is_zero_result=False,
            show_more_exhausted=False,
            total_latency_ms=1,
            served_top_ids=[str(ids["resume"])],
            served_owner_count=1,
            served_max_owner_items=1,
            served_exploration_count=0,
        ))
        db.flush()
        delivery = RecommendationDelivery(
            delivery_id=delivery_id,
            delivery_order=1,
            source_inbound_msg_id=source_id,
            reply_index=0,
            request_id=request_id,
            userid=owner,
            content_ciphertext=b"not-sent",
            content_expires_at=now + timedelta(days=1),
            recommendation_context={
                "items": [{"target_type": "resume", "target_id": ids["resume"]}],
            },
            status="pending",
            session_commit_token=str(uuid4()),
            next_attempt_at=now,
            impression_next_attempt_at=now,
        )
        db.add(delivery)
        db.flush()
        outbox = WecomOutboundOutbox(
            inbound_event_id=ids["inbound"],
            reply_index=0,
            userid=owner,
            msg_type="text",
            content=None,
            recommendation_delivery_id=delivery_id,
            status="pending",
        )
        task = TargetCleanupTask(
            operation_id=str(uuid4()),
            target_type="resume",
            target_id=ids["resume"],
            reason="expired",
            reason_history=["expired"],
            status="processing",
            attempt_count=1,
            lease_owner=worker_owner,
            lease_expires_at=now + timedelta(minutes=4),
        )
        db.add_all([outbox, task])
        db.commit()
        ids["outbox"] = int(outbox.id)
        ids["task"] = int(task.id)

    cleanup_has_task = Event()
    allow_cleanup_outbox = Event()
    expiry_has_resume = Event()
    allow_expiry_task = Event()
    expiry_task_attempted = Event()
    dispatcher_resume_attempted = Event()
    results: dict[str, object] = {}
    failures: list[BaseException] = []

    original_lock_outboxes = privacy_service._lock_outboxes_for_deliveries
    original_ensure_task = expiry_service.ensure_target_cleanup_task

    def gated_lock_outboxes(db, delivery_ids):
        cleanup_has_task.set()
        assert allow_cleanup_outbox.wait(10)
        return original_lock_outboxes(db, delivery_ids)

    def gated_ensure_task(db, target_type, target_id, **kwargs):
        expiry_has_resume.set()
        assert allow_expiry_task.wait(10)
        return original_ensure_task(db, target_type, target_id, **kwargs)

    monkeypatch.setattr(
        privacy_service, "_lock_outboxes_for_deliveries", gated_lock_outboxes,
    )
    monkeypatch.setattr(expiry_service, "ensure_target_cleanup_task", gated_ensure_task)

    def observe_lock_attempts(_conn, _cursor, statement, _params, _ctx, _many):
        normalized = " ".join(statement.lower().split())
        name = current_thread().name
        if name == "stage5-expiry" and "target_cleanup_task" in normalized and "for update" in normalized:
            expiry_task_attempted.set()
        if name == "stage5-dispatcher" and " from resume " in f" {normalized} " and "for update" in normalized:
            dispatcher_resume_attempted.set()

    event.listen(engine, "before_cursor_execute", observe_lock_attempts)

    def run_cleanup() -> None:
        current_thread().name = "stage5-cleanup"
        try:
            with SessionLocal() as db:
                results["cleanup"] = process_cleanup_task(
                    db, ids["task"], worker_owner,
                )
        except BaseException as exc:
            failures.append(exc)

    def run_expiry() -> None:
        current_thread().name = "stage5-expiry"
        try:
            with SessionLocal() as db:
                results["expiry"] = expire_locked_batch(db, now=now, batch_size=1)
        except BaseException as exc:
            failures.append(exc)

    def run_dispatcher() -> None:
        current_thread().name = "stage5-dispatcher"
        try:
            results["dispatcher"] = Worker.__new__(Worker)._claim_outbox(limit=1)
        except BaseException as exc:
            failures.append(exc)

    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            cleanup_future = pool.submit(run_cleanup)
            assert cleanup_has_task.wait(10)
            expiry_future = pool.submit(run_expiry)
            assert expiry_has_resume.wait(10)
            dispatcher_future = pool.submit(run_dispatcher)
            assert dispatcher_resume_attempted.wait(10)

            # Let expiry request the task while cleanup still owns it.  The
            # dispatcher is already waiting on Resume and owns no outbox lock.
            allow_expiry_task.set()
            assert expiry_task_attempted.wait(10)
            allow_cleanup_outbox.set()

            cleanup_future.result(timeout=15)
            expiry_future.result(timeout=15)
            dispatcher_future.result(timeout=15)

        assert failures == []
        assert results == {
            "cleanup": True,
            "expiry": [ids["resume"]],
            "dispatcher": [],
        }
        with SessionLocal() as db:
            resume = db.get(Resume, ids["resume"])
            task = db.get(TargetCleanupTask, ids["task"])
            outbox = db.get(WecomOutboundOutbox, ids["outbox"])
            delivery = db.get(RecommendationDelivery, delivery_id)
            assert resume.delist_reason == "expired"
            assert resume.deleted_at is not None
            assert task.status == "succeeded"
            assert task.db_redacted_at is not None
            assert outbox.status == "dead_letter"
            assert delivery.status == "permanent_failed"
            assert delivery.content_ciphertext is None
    finally:
        allow_expiry_task.set()
        allow_cleanup_outbox.set()
        event.remove(engine, "before_cursor_execute", observe_lock_attempts)
        with SessionLocal() as db:
            db.query(WecomOutboundOutbox).filter_by(id=ids.get("outbox")).delete(
                synchronize_session=False,
            )
            db.query(RecommendationDelivery).filter_by(
                delivery_id=delivery_id,
            ).delete(synchronize_session=False)
            db.query(RecommendationRequest).filter_by(
                request_id=request_id,
            ).delete(synchronize_session=False)
            db.query(TargetCleanupTask).filter_by(id=ids.get("task")).delete(
                synchronize_session=False,
            )
            db.query(Resume).filter_by(id=ids.get("resume")).delete(
                synchronize_session=False,
            )
            db.query(WecomInboundEvent).filter_by(id=ids.get("inbound")).delete(
                synchronize_session=False,
            )
            db.query(User).filter_by(external_userid=owner).delete(
                synchronize_session=False,
            )
            db.commit()


def _resume(owner: str) -> int:
    now = datetime.utcnow().replace(microsecond=0)
    with SessionLocal() as db:
        db.add(User(external_userid=owner, role="worker"))
        db.flush()
        row = Resume(
            owner_userid=owner,
            expected_cities=["苏州"],
            expected_job_categories=["普工"],
            salary_expect_floor_monthly=5000,
            gender="男",
            age=30,
            accept_long_term=True,
            accept_short_term=False,
            raw_text="stage5 fence",
            audit_status="passed",
            activated_at=now - timedelta(days=1),
            expires_at=now + timedelta(days=29),
            candidate_expires_at=None,
        )
        db.add(row)
        db.commit()
        return int(row.id)


def _cleanup(owner: str, resume_id: int, request_id: str | None = None) -> None:
    redis = get_redis()
    target_index = f"{RECOMMENDATION_SESSION_TARGET_INDEX_PREFIX}resume:{resume_id}"
    redis.delete(target_index)
    redis.srem(RECOMMENDATION_SESSION_REVOCATION_FENCE_KEY, target_index)
    with SessionLocal() as db:
        if request_id:
            db.query(RecommendationSearchAttempt).filter_by(
                request_id=request_id,
            ).delete(synchronize_session=False)
            db.query(RecommendationRequest).filter_by(
                request_id=request_id,
            ).delete(synchronize_session=False)
        db.query(TargetCleanupTask).filter_by(
            target_type="resume", target_id=resume_id,
        ).delete(synchronize_session=False)
        db.query(Resume).filter_by(id=resume_id).delete(synchronize_session=False)
        db.query(User).filter_by(external_userid=owner).delete(synchronize_session=False)
        db.commit()


@pytest.mark.parametrize(
    "target_fact",
    [
        {"precision_pool_ids": "top"},
        {"additional_attempts": "candidate"},
        {"additional_attempts": "precision"},
    ],
    ids=("precision-pool", "additional-candidate", "additional-precision"),
)
def test_cleanup_commit_first_rejects_late_recommendation_fact_with_stable_code(
    target_fact,
):
    owner = f"stage5-late-{uuid4().hex}"
    resume_id = _resume(owner)
    request_id = str(uuid4())
    expiry_locked = Event()
    writer_entered = Event()
    release_expiry = Event()
    failures: list[BaseException] = []
    stale_codes: list[str] = []

    if "precision_pool_ids" in target_fact:
        target_fields = {"precision_pool_ids": [str(resume_id)]}
    else:
        additional_key = (
            "candidate_ids" if target_fact["additional_attempts"] == "candidate"
            else "precision_pool_ids"
        )
        target_fields = {
            "additional_attempts": [{additional_key: [str(resume_id)]}],
        }

    def expire_first() -> None:
        with SessionLocal() as db:
            try:
                row = db.query(Resume).filter_by(id=resume_id).with_for_update().one()
                row.delist_reason = "expired"
                row.deleted_at = datetime.utcnow()
                expiry_locked.set()
                assert release_expiry.wait(10)
                db.commit()
            except BaseException as exc:
                failures.append(exc)
                db.rollback()

    def late_writer() -> None:
        assert expiry_locked.wait(10)
        with SessionLocal() as db:
            writer_entered.set()
            try:
                persist_request_fact_only(
                    db,
                    inbound_event_id=1,
                    reply_index=0,
                    userid=owner,
                    source_inbound_msg_id=f"stage5-{uuid4().hex}",
                    now=datetime.utcnow(),
                    request_fact={
                        "request_id": request_id,
                        "request_kind": "initial_search",
                        "viewer_userid": owner,
                        "direction": "search_worker",
                        "query_digest": "stage5-late-write",
                        "algorithm_version": "legacy",
                        "candidate_ids": [],
                        "served_top_ids": [],
                        "candidate_count": 0,
                        "result_count": 0,
                        "execution_mode": "off",
                        "served_assignment": "legacy",
                        **target_fields,
                    },
                )
                db.commit()
            except RecommendationTargetStale as exc:
                stale_codes.append(exc.code)
                db.rollback()
            except BaseException as exc:
                failures.append(exc)
                db.rollback()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(expire_first)
            second = pool.submit(late_writer)
            assert writer_entered.wait(10)
            release_expiry.set()
            first.result(timeout=10)
            second.result(timeout=10)
        assert failures == []
        assert stale_codes == ["recommendation_target_stale"]
        with SessionLocal() as db:
            assert db.get(RecommendationRequest, request_id) is None
            assert db.query(RecommendationSearchAttempt).filter_by(
                request_id=request_id,
            ).count() == 0
    finally:
        release_expiry.set()
        _cleanup(owner, resume_id, request_id)


def test_resume_cleanup_sets_redis_fence_before_succeeding():
    owner = f"stage5-fence-{uuid4().hex}"
    resume_id = _resume(owner)
    target_index = f"{RECOMMENDATION_SESSION_TARGET_INDEX_PREFIX}resume:{resume_id}"
    redis = get_redis()
    redis.delete(target_index)
    redis.srem(RECOMMENDATION_SESSION_REVOCATION_FENCE_KEY, target_index)
    task_id = None
    worker = f"stage5-worker-{uuid4().hex}"
    try:
        with SessionLocal() as db:
            task = TargetCleanupTask(
                operation_id=str(uuid4()),
                target_type="resume",
                target_id=resume_id,
                reason="expired",
                reason_history=["expired"],
                status="processing",
                attempt_count=1,
                lease_owner=worker,
                lease_expires_at=datetime.utcnow() + timedelta(minutes=4),
            )
            db.add(task)
            db.commit()
            task_id = int(task.id)

        with SessionLocal() as db:
            assert process_cleanup_task(db, task_id, worker)
        assert redis.sismember(
            RECOMMENDATION_SESSION_REVOCATION_FENCE_KEY, target_index,
        )
        with SessionLocal() as db:
            completed = db.get(TargetCleanupTask, task_id)
            assert completed.status == "succeeded"
            assert completed.db_redacted_at is not None
            assert completed.conversation_redacted_at is not None
            assert completed.session_invalidated_at is not None
    finally:
        _cleanup(owner, resume_id)


def test_recommendation_commit_first_is_then_removed_by_cleanup():
    owner = f"stage5-write-first-{uuid4().hex}"
    resume_id = _resume(owner)
    request_id = str(uuid4())
    writer_locked = Event()
    cleanup_entered = Event()
    cleanup_skipped = Event()
    release_writer = Event()
    failures: list[BaseException] = []

    def writer_first() -> None:
        with SessionLocal() as db:
            try:
                persist_request_fact_only(
                    db,
                    inbound_event_id=1,
                    reply_index=0,
                    userid=owner,
                    source_inbound_msg_id=f"stage5-{uuid4().hex}",
                    now=datetime.utcnow(),
                    request_fact={
                        "request_id": request_id,
                        "request_kind": "initial_search",
                        "viewer_userid": owner,
                        "direction": "search_worker",
                        "query_digest": "stage5-write-first",
                        "algorithm_version": "legacy",
                        "candidate_ids": [str(resume_id)],
                        "served_top_ids": [str(resume_id)],
                        "candidate_count": 1,
                        "result_count": 1,
                        "execution_mode": "off",
                        "served_assignment": "legacy",
                    },
                )
                writer_locked.set()
                assert release_writer.wait(10)
                db.commit()
            except BaseException as exc:
                failures.append(exc)
                db.rollback()

    def cleanup_second() -> None:
        assert writer_locked.wait(10)
        with SessionLocal() as db:
            try:
                cleanup_entered.set()
                assert expire_locked_batch(
                    db, now=datetime.utcnow() + timedelta(days=31), batch_size=1,
                ) == []
                cleanup_skipped.set()
            except BaseException as exc:
                failures.append(exc)
                db.rollback()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(writer_first)
            second = pool.submit(cleanup_second)
            assert cleanup_entered.wait(10)
            assert cleanup_skipped.wait(10)
            release_writer.set()
            first.result(timeout=10)
            second.result(timeout=10)
        assert failures == []

        # SKIP LOCKED makes the first cleanup invocation non-blocking.  Its
        # next bounded invocation observes the just-committed fact and expires
        # the Resume, after which the common cleanup task removes that fact.
        with SessionLocal() as db:
            assert expire_locked_batch(
                db, now=datetime.utcnow() + timedelta(days=31), batch_size=1,
            ) == [resume_id]

        worker = f"stage5-clean-{uuid4().hex}"
        with SessionLocal() as db:
            task = db.query(TargetCleanupTask).filter_by(
                target_type="resume", target_id=resume_id,
            ).one()
            task.status = "processing"
            task.attempt_count = 1
            task.lease_owner = worker
            task.lease_expires_at = datetime.utcnow() + timedelta(minutes=4)
            task_id = int(task.id)
            db.commit()
        with SessionLocal() as db:
            assert process_cleanup_task(db, task_id, worker)
        with SessionLocal() as db:
            request = db.get(RecommendationRequest, request_id)
            attempt = db.query(RecommendationSearchAttempt).filter_by(
                request_id=request_id,
            ).one()
            assert request.served_top_ids == []
            assert attempt.candidate_ids == []
            assert db.get(TargetCleanupTask, task_id).status == "succeeded"
    finally:
        release_writer.set()
        _cleanup(owner, resume_id, request_id)


def test_expiry_and_candidate_recovery_keep_distinct_terminal_reasons():
    """One real-MySQL batch proves the two lifecycle predicates do not overlap."""
    owner = f"stage5-batch-{uuid4().hex}"
    now = datetime.utcnow().replace(microsecond=0)
    active_id = candidate_id = None
    with SessionLocal() as db:
        db.add(User(external_userid=owner, role="worker"))
        db.flush()
        common = dict(
            owner_userid=owner,
            expected_cities=["苏州"],
            expected_job_categories=["普工"],
            salary_expect_floor_monthly=5000,
            gender="男",
            age=30,
            accept_long_term=True,
            accept_short_term=False,
        )
        active = Resume(
            **common,
            raw_text="stage5 active",
            audit_status="passed",
            activated_at=now - timedelta(days=30),
            expires_at=now,
            candidate_expires_at=None,
        )
        candidate = Resume(
            **common,
            raw_text="stage5 candidate",
            audit_status="pending",
            activated_at=None,
            expires_at=None,
            candidate_expires_at=now,
        )
        db.add_all([active, candidate])
        db.commit()
        active_id, candidate_id = int(active.id), int(candidate.id)

    try:
        with SessionLocal() as db:
            assert expire_locked_batch(db, now=now, batch_size=1) == [active_id]
        with SessionLocal() as db:
            assert cleanup_candidate(db, candidate_id, now=now)
            db.commit()
        with SessionLocal() as db:
            active = db.get(Resume, active_id)
            candidate = db.get(Resume, candidate_id)
            assert active.delist_reason == "expired"
            assert candidate.delist_reason == "candidate_expired"
            reasons = {
                (row.target_id, row.reason)
                for row in db.query(TargetCleanupTask).filter(
                    TargetCleanupTask.target_type == "resume",
                    TargetCleanupTask.target_id.in_([active_id, candidate_id]),
                )
            }
            assert reasons == {
                (active_id, "expired"),
                (candidate_id, "candidate_expired"),
            }
    finally:
        with SessionLocal() as db:
            db.query(TargetCleanupTask).filter(
                TargetCleanupTask.target_type == "resume",
                TargetCleanupTask.target_id.in_([active_id, candidate_id]),
            ).delete(synchronize_session=False)
            db.query(Resume).filter(Resume.id.in_([active_id, candidate_id])).delete(
                synchronize_session=False,
            )
            db.query(User).filter_by(external_userid=owner).delete(
                synchronize_session=False,
            )
            db.commit()
