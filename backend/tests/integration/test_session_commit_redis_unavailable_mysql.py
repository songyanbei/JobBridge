"""Fail-closed durable session terminalization during a real Redis outage."""
from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from app.db import SessionLocal
from app.core.redis_client import user_lock
from app.models import (
    RecommendationDelivery,
    RecommendationRequest,
    WecomInboundEvent,
    WecomOutboundOutbox,
)
from app.services.worker import Worker
from .recommendation_integration_support import delivery, request


pytestmark = pytest.mark.integration


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        check=check,
        capture_output=True,
        text=True,
        timeout=20,
    )


def _start_redis(container: str) -> None:
    _docker("start", container)
    for _ in range(50):
        result = _docker(
            "exec", container, "redis-cli", "ping", check=False,
        )
        if result.returncode == 0 and result.stdout.strip() == "PONG":
            return
        time.sleep(0.1)
    raise RuntimeError(f"Redis container did not become ready: {container}")


def test_expired_session_commit_terminalizes_when_redis_is_stopped():
    container = os.environ.get("REDIS_OUTAGE_CONTAINER")
    if not container:
        pytest.skip("set REDIS_OUTAGE_CONTAINER to an isolated Redis container")

    db = SessionLocal()
    event_id = None
    delivery_id = str(uuid4())
    request_id = str(uuid4())
    source_id = f"redis-outage-{uuid4()}"
    userid = f"redis-outage-user-{uuid4().hex[:8]}"
    redis_stopped = False
    try:
        event = WecomInboundEvent(
            msg_id=source_id,
            from_userid=userid,
            msg_type="text",
            content_brief="redis outage terminalization",
            status="session_pending",
            session_operation="save",
            session_expected_version=0,
            session_payload=None,
            session_next_attempt_at=(
                datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
            ),
            session_commit_deadline_epoch=Decimal("1.000000"),
        )
        db.add(event)
        db.add(request(request_id, source_id, userid))
        db.flush()
        event_id = int(event.id)

        recommendation = delivery(
            delivery_id,
            source_id,
            request_id,
            userid,
            status="prepared",
            content_ciphertext=b"encrypted-body",
            session_patch_ciphertext=b"encrypted-session-patch",
            session_commit_state="not_applied",
        )
        db.add(recommendation)
        db.flush()
        db.add(WecomOutboundOutbox(
            inbound_event_id=event_id,
            reply_index=0,
            userid=userid,
            msg_type="text",
            content=None,
            recommendation_delivery_id=delivery_id,
            status="pending",
        ))
        db.commit()

        _docker("stop", "-t", "1", container)
        redis_stopped = True

        with user_lock(userid, timeout=0) as outage_lease:
            assert not outage_lease
            assert outage_lease.unavailable is True

        worker = object.__new__(Worker)
        assert worker.reconcile_sessions_once(limit=1) == 1

        db.expire_all()
        terminal_event = db.get(WecomInboundEvent, event_id)
        terminal_delivery = db.get(RecommendationDelivery, delivery_id)
        terminal_outbox = db.query(WecomOutboundOutbox).filter(
            WecomOutboundOutbox.inbound_event_id == event_id,
        ).one()

        assert terminal_event.status == "dead_letter"
        assert terminal_event.session_operation is None
        assert terminal_event.session_payload is None
        assert terminal_event.session_apply_lease_owner is None
        assert terminal_event.session_commit_deadline_epoch is None
        assert terminal_delivery.status == "permanent_failed"
        assert terminal_delivery.last_error_code == "session_commit_deadline"
        assert terminal_delivery.content_ciphertext is None
        assert terminal_delivery.session_patch_ciphertext is None
        assert terminal_outbox.status == "dead_letter"
        assert terminal_outbox.locked_at is None
        assert terminal_outbox.next_attempt_at is None
    finally:
        try:
            if redis_stopped:
                _start_redis(container)
        finally:
            db.rollback()
            if event_id is not None:
                db.query(WecomOutboundOutbox).filter(
                    WecomOutboundOutbox.inbound_event_id == event_id,
                ).delete(synchronize_session=False)
            db.query(RecommendationDelivery).filter(
                RecommendationDelivery.delivery_id == delivery_id,
            ).delete(synchronize_session=False)
            db.query(RecommendationRequest).filter(
                RecommendationRequest.request_id == request_id,
            ).delete(synchronize_session=False)
            if event_id is not None:
                db.query(WecomInboundEvent).filter(
                    WecomInboundEvent.id == event_id,
                ).delete(synchronize_session=False)
            db.commit()
            db.close()
