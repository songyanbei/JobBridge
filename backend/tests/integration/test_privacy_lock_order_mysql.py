"""Real MySQL evidence for privacy outbox-to-delivery lock order."""
from __future__ import annotations

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
from app.services.recommendation_privacy_service import (
    TargetRef,
    redact_deliveries_for_targets,
)

from .recommendation_integration_support import delivery, request, user


pytestmark = pytest.mark.integration


def test_target_redaction_locks_outbox_before_delivery():
    prefix = uuid4().hex[:12]
    userid = f"privacy-lock-{prefix}"
    source_id = f"privacy-lock-source-{prefix}"
    request_id = str(uuid4())
    delivery_id = str(uuid4())
    target_id = int(uuid4().int % 9_000_000_000) + 30_000_000_000
    db = SessionLocal()
    engine = db.get_bind()
    locked_tables = []

    def _record_lock_sql(
        _conn, _cursor, statement, _parameters, _context, _executemany,
    ):
        normalized = " ".join(statement.lower().split())
        if "for update" not in normalized:
            return
        if "from wecom_outbound_outbox" in normalized:
            locked_tables.append("outbox")
        elif "from recommendation_delivery" in normalized:
            locked_tables.append("delivery")

    try:
        db.add(user(userid))
        inbound = WecomInboundEvent(
            msg_id=source_id,
            from_userid=userid,
            msg_type="text",
            content_brief="privacy lock order",
            status="done",
        )
        db.add(inbound)
        db.add(request(request_id, source_id, userid))
        db.flush()
        recommendation = delivery(
            delivery_id,
            source_id,
            request_id,
            userid,
            status="pending",
            content_ciphertext=b"encrypted-content",
            session_patch_ciphertext=b"encrypted-session-patch",
            recommendation_context={
                "direction": "search_job",
                "served_top_ids": [str(target_id)],
                "items": [{
                    "target_type": "job",
                    "target_id": target_id,
                    "position": 1,
                }],
            },
        )
        db.add(recommendation)
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

        event.listen(engine, "before_cursor_execute", _record_lock_sql)
        touched = redact_deliveries_for_targets(
            db,
            [TargetRef("job", target_id)],
            commit=False,
        )
        db.commit()
        event.remove(engine, "before_cursor_execute", _record_lock_sql)

        assert touched == {delivery_id}
        assert locked_tables[:2] == ["outbox", "delivery"]
        db.expire_all()
        saved_delivery = db.get(RecommendationDelivery, delivery_id)
        saved_outbox = db.query(WecomOutboundOutbox).filter(
            WecomOutboundOutbox.recommendation_delivery_id == delivery_id,
        ).one()
        assert saved_delivery.status == "permanent_failed"
        assert saved_outbox.status == "dead_letter"
    finally:
        if event.contains(engine, "before_cursor_execute", _record_lock_sql):
            event.remove(engine, "before_cursor_execute", _record_lock_sql)
        db.rollback()
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
        db.close()
