"""Real MySQL evidence for TTL outbox-to-delivery lock ordering."""
from __future__ import annotations

from datetime import timedelta
import threading
from uuid import uuid4

import pytest
from sqlalchemy import event

from app.db import SessionLocal
from app.models import (
    RecommendationDelivery,
    RecommendationRequest,
    User,
    WecomInboundEvent,
    WecomOutboundOutbox,
)
from app.services.worker import Worker
from app.tasks.ttl_cleanup import _redact_expired_recommendation_content

from .recommendation_integration_support import (
    delivery,
    naive_utc_now,
    request,
    user,
)


pytestmark = pytest.mark.integration


def _delete_fixture_rows(db, *, delivery_id, request_id, source_id, userid):
    db.query(WecomOutboundOutbox).filter(
        WecomOutboundOutbox.recommendation_delivery_id == delivery_id,
    ).delete(synchronize_session=False)
    db.query(RecommendationDelivery).filter(
        RecommendationDelivery.delivery_id == delivery_id,
    ).delete(synchronize_session=False)
    db.query(RecommendationRequest).filter(
        RecommendationRequest.request_id == request_id,
    ).delete(synchronize_session=False)
    db.query(WecomInboundEvent).filter(
        WecomInboundEvent.msg_id == source_id,
    ).delete(synchronize_session=False)
    db.query(User).filter(
        User.external_userid == userid,
    ).delete(synchronize_session=False)
    db.commit()


def test_ttl_locks_outbox_before_delivery_and_uses_single_table_writes(
    monkeypatch,
):
    prefix = uuid4().hex[:12]
    userid = f"ttl-lock-{prefix}"
    source_id = f"ttl-lock-source-{prefix}"
    request_id = str(uuid4())
    delivery_id = str(uuid4())
    db = SessionLocal()
    engine = db.get_bind()
    observed = []

    def _record_sql(_conn, _cursor, statement, _params, _context, _many):
        normalized = " ".join(statement.lower().split())
        if "for update" in normalized:
            if "from wecom_outbound_outbox" in normalized:
                observed.append("lock:outbox")
            elif "from recommendation_delivery" in normalized:
                observed.append("lock:delivery")
        if normalized.startswith("update "):
            observed.append(normalized)

    try:
        db.add(user(userid))
        inbound = WecomInboundEvent(
            msg_id=source_id,
            from_userid=userid,
            msg_type="text",
            content_brief="ttl lock order",
            status="done",
        )
        db.add(inbound)
        db.add(request(request_id, source_id, userid))
        db.flush()
        db.add(delivery(
            delivery_id,
            source_id,
            request_id,
            userid,
            status="retry_wait",
            content_ciphertext=b"encrypted-content",
            session_patch_ciphertext=b"encrypted-patch",
            content_expires_at=naive_utc_now() - timedelta(seconds=1),
        ))
        db.flush()
        db.add(WecomOutboundOutbox(
            inbound_event_id=int(inbound.id),
            reply_index=0,
            userid=userid,
            msg_type="text",
            recommendation_delivery_id=delivery_id,
            status="pending",
        ))
        db.commit()

        monkeypatch.setattr(
            "app.tasks.ttl_cleanup._expired_content_candidate_ids",
            lambda _db, _after_id: [delivery_id],
        )
        event.listen(engine, "before_cursor_execute", _record_sql)
        assert _redact_expired_recommendation_content(db) == 1
        event.remove(engine, "before_cursor_execute", _record_sql)

        assert observed[:2] == ["lock:outbox", "lock:delivery"]
        updates = [sql for sql in observed if sql.startswith("update ")]
        assert updates
        assert all(not (
            "recommendation_delivery" in sql
            and "wecom_outbound_outbox" in sql
        ) for sql in updates)
        db.expire_all()
        saved_delivery = db.get(RecommendationDelivery, delivery_id)
        saved_outbox = db.query(WecomOutboundOutbox).filter(
            WecomOutboundOutbox.recommendation_delivery_id == delivery_id,
        ).one()
        assert saved_delivery.status == "permanent_failed"
        assert saved_delivery.content_ciphertext is None
        assert saved_delivery.session_patch_ciphertext is None
        assert saved_outbox.status == "dead_letter"
    finally:
        if event.contains(engine, "before_cursor_execute", _record_sql):
            event.remove(engine, "before_cursor_execute", _record_sql)
        db.rollback()
        _delete_fixture_rows(
            db,
            delivery_id=delivery_id,
            request_id=request_id,
            source_id=source_id,
            userid=userid,
        )
        db.close()


def test_ttl_and_session_terminalizer_converge_without_deadlock(monkeypatch):
    prefix = uuid4().hex[:12]
    userid = f"ttl-race-{prefix}"
    source_id = f"ttl-race-source-{prefix}"
    request_id = str(uuid4())
    delivery_id = str(uuid4())
    setup_db = SessionLocal()
    terminal_db = SessionLocal()
    ttl_started_lock = threading.Event()
    ttl_errors = []
    ttl_result = []
    ttl_thread = None
    engine = setup_db.get_bind()

    def _observe_ttl_lock(_conn, _cursor, statement, _params, _context, _many):
        normalized = " ".join(statement.lower().split())
        if (
            "for update" in normalized
            and "from wecom_outbound_outbox" in normalized
            and threading.current_thread() is ttl_thread
        ):
            ttl_started_lock.set()

    try:
        setup_db.add(user(userid))
        inbound = WecomInboundEvent(
            msg_id=source_id,
            from_userid=userid,
            msg_type="text",
            content_brief="ttl terminalizer race",
            status="session_pending",
            session_operation="delete",
            session_expected_version=0,
            session_payload={},
            session_commit_deadline_epoch=0,
        )
        setup_db.add(inbound)
        setup_db.add(request(request_id, source_id, userid))
        setup_db.flush()
        event_id = int(inbound.id)
        setup_db.add(delivery(
            delivery_id,
            source_id,
            request_id,
            userid,
            status="prepared",
            content_ciphertext=b"encrypted-content",
            session_patch_ciphertext=b"encrypted-patch",
            content_expires_at=naive_utc_now() - timedelta(seconds=1),
        ))
        setup_db.flush()
        setup_db.add(WecomOutboundOutbox(
            inbound_event_id=event_id,
            reply_index=0,
            userid=userid,
            msg_type="text",
            recommendation_delivery_id=delivery_id,
            status="pending",
        ))
        setup_db.commit()

        locked_inbound = terminal_db.query(WecomInboundEvent).filter(
            WecomInboundEvent.id == event_id,
        ).with_for_update().one()
        terminal_db.query(WecomOutboundOutbox).filter(
            WecomOutboundOutbox.recommendation_delivery_id == delivery_id,
        ).with_for_update().one()

        monkeypatch.setattr(
            "app.tasks.ttl_cleanup._expired_content_candidate_ids",
            lambda _db, _after_id: [delivery_id],
        )

        def _run_ttl():
            db = SessionLocal()
            try:
                ttl_result.append(_redact_expired_recommendation_content(db))
            except Exception as exc:
                ttl_errors.append(exc)
            finally:
                db.rollback()
                db.close()

        event.listen(engine, "before_cursor_execute", _observe_ttl_lock)
        ttl_thread = threading.Thread(target=_run_ttl)
        ttl_thread.start()
        assert ttl_started_lock.wait(timeout=5)

        worker = Worker.__new__(Worker)
        worker._terminalize_session_commit_locked(
            terminal_db,
            locked_inbound,
            error_code="session_commit_deadline",
            error=RuntimeError("forced terminalization during TTL cleanup"),
        )
        terminal_db.commit()
        ttl_thread.join(timeout=10)
        event.remove(engine, "before_cursor_execute", _observe_ttl_lock)

        assert not ttl_thread.is_alive()
        assert ttl_errors == []
        assert ttl_result in ([0], [1])
        setup_db.expire_all()
        saved_delivery = setup_db.get(RecommendationDelivery, delivery_id)
        saved_outbox = setup_db.query(WecomOutboundOutbox).filter(
            WecomOutboundOutbox.recommendation_delivery_id == delivery_id,
        ).one()
        saved_inbound = setup_db.get(WecomInboundEvent, event_id)
        assert saved_delivery.status == "permanent_failed"
        assert saved_delivery.content_ciphertext is None
        assert saved_delivery.session_patch_ciphertext is None
        assert saved_outbox.status == "dead_letter"
        assert saved_inbound.status == "dead_letter"
    finally:
        if event.contains(engine, "before_cursor_execute", _observe_ttl_lock):
            event.remove(engine, "before_cursor_execute", _observe_ttl_lock)
        terminal_db.rollback()
        terminal_db.close()
        if ttl_thread is not None:
            ttl_thread.join(timeout=10)
        setup_db.rollback()
        _delete_fixture_rows(
            setup_db,
            delivery_id=delivery_id,
            request_id=request_id,
            source_id=source_id,
            userid=userid,
        )
        setup_db.close()
