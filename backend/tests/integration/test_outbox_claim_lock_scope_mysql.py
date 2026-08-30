"""Real MySQL evidence that outbox claims lock only outbox rows."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from uuid import uuid4
from unittest.mock import MagicMock

import pytest

from app.db import SessionLocal
from app.models import (
    ContactAccessAudit,
    ContactDelivery,
    ContactGrant,
    ContactRequest,
    WecomInboundEvent,
    WecomOutboundOutbox,
)
from app.services.worker import Worker


pytestmark = pytest.mark.integration


def test_inbound_row_lock_does_not_block_outbox_claim():
    source_id = f"outbox-lock-scope-{uuid4()}"
    userid = f"outbox-lock-user-{uuid4().hex[:12]}"
    setup_db = SessionLocal()
    blocker_db = SessionLocal()
    event_id = None
    outbox_id = None
    claimed_result = []
    claim_errors = []
    claim_thread = None
    try:
        inbound = WecomInboundEvent(
            msg_id=source_id,
            from_userid=userid,
            msg_type="text",
            content_brief="outbox lock scope",
            status="done",
        )
        setup_db.add(inbound)
        setup_db.flush()
        event_id = int(inbound.id)
        outbox = WecomOutboundOutbox(
            inbound_event_id=event_id,
            reply_index=0,
            userid=userid,
            msg_type="text",
            content="claimable while inbound is locked",
            status="pending",
        )
        setup_db.add(outbox)
        setup_db.commit()
        outbox_id = int(outbox.id)

        blocker_db.query(WecomInboundEvent).filter(
            WecomInboundEvent.id == event_id,
        ).with_for_update().one()

        def _claim():
            try:
                worker = Worker.__new__(Worker)
                claimed_result.extend(worker._claim_outbox(
                    inbound_event_id=event_id,
                    limit=1,
                ))
            except Exception as exc:
                claim_errors.append(exc)

        claim_thread = threading.Thread(target=_claim)
        claim_thread.start()
        claim_thread.join(timeout=5)
        completed_while_parent_locked = not claim_thread.is_alive()

        blocker_db.rollback()
        claim_thread.join(timeout=10)

        assert completed_while_parent_locked
        assert not claim_thread.is_alive()
        assert claim_errors == []
        assert len(claimed_result) == 1
        assert claimed_result[0]["id"] == outbox_id
        assert claimed_result[0]["content"] == "claimable while inbound is locked"
    finally:
        blocker_db.rollback()
        blocker_db.close()
        if claim_thread is not None:
            claim_thread.join(timeout=10)
        setup_db.rollback()
        if outbox_id is not None:
            setup_db.query(WecomOutboundOutbox).filter(
                WecomOutboundOutbox.id == outbox_id,
            ).delete(synchronize_session=False)
        if event_id is not None:
            setup_db.query(WecomInboundEvent).filter(
                WecomInboundEvent.id == event_id,
            ).delete(synchronize_session=False)
        setup_db.commit()
        setup_db.close()


def test_contact_outbox_claims_delivery_and_sends_fixed_platform_request():
    source_id = f"contact-outbox-{uuid4()}"
    userid = f"contact-outbox-user-{uuid4().hex[:12]}"
    request_id = f"cr_{uuid4().hex}"
    grant_id = f"cg_{uuid4().hex}"
    delivery_id = f"cd_{uuid4().hex}"
    db = SessionLocal()
    event_id = outbox_id = None
    try:
        inbound = WecomInboundEvent(
            msg_id=source_id,
            from_userid=userid,
            msg_type="text",
            content_brief="contact request",
            status="done",
        )
        db.add(inbound)
        db.flush()
        event_id = int(inbound.id)
        db.add(ContactRequest(
            request_id=request_id,
            actor_id=userid,
            listing_ref="recruitment.job:1",
            action="request_contact",
            request_digest="a" * 64,
            nonce_digest="b" * 64,
            status="authorized",
            expires_at=datetime.utcnow() + timedelta(minutes=5),
        ))
        db.add(ContactGrant(
            grant_id=grant_id,
            request_id=request_id,
            actor_id=userid,
            listing_ref="recruitment.job:1",
            action="request_contact",
            token_hash="c" * 64,
            nonce_digest="d" * 64,
            status="used",
            expires_at=datetime.utcnow() + timedelta(minutes=5),
            used_at=datetime.utcnow(),
        ))
        db.add(ContactDelivery(
            delivery_id=delivery_id,
            grant_id=grant_id,
            actor_id=userid,
            listing_ref="recruitment.job:1",
            channel="platform_request",
            content_ciphertext=None,
            status="prepared",
            expires_at=datetime.utcnow() + timedelta(minutes=5),
        ))
        outbox = WecomOutboundOutbox(
            inbound_event_id=event_id,
            reply_index=0,
            userid=userid,
            msg_type="text",
            content=None,
            contact_delivery_id=delivery_id,
            intent="contact_request",
            status="pending",
        )
        db.add(outbox)
        db.commit()
        outbox_id = int(outbox.id)

        worker = Worker.__new__(Worker)
        worker._lease_owner = "integration-contact-worker"
        claimed = worker._claim_outbox(inbound_event_id=event_id, limit=1)

        assert len(claimed) == 1
        item = claimed[0]
        assert item["contact_delivery_id"] == delivery_id
        assert item["recommendation_delivery_id"] is None
        assert item["content"]
        assert "联系请求已提交" in item["content"]
        db.expire_all()
        assert db.query(ContactDelivery).filter_by(delivery_id=delivery_id).one().status == "sending"

        worker._wecom_client = MagicMock()
        worker._wecom_client.send_text.return_value = {"errcode": 0, "msgid": "contact-msg-1"}
        assert worker._deliver_outbox_item(item) is True

        db.expire_all()
        assert db.query(WecomOutboundOutbox).filter_by(id=outbox_id).one().status == "sent"
        assert db.query(ContactDelivery).filter_by(delivery_id=delivery_id).one().status == "sent"
        worker._wecom_client.send_text.assert_called_once_with(userid, item["content"])
    finally:
        db.rollback()
        if outbox_id is not None:
            db.query(WecomOutboundOutbox).filter(WecomOutboundOutbox.id == outbox_id).delete(synchronize_session=False)
        db.query(ContactDelivery).filter(ContactDelivery.delivery_id == delivery_id).delete(synchronize_session=False)
        db.query(ContactGrant).filter(ContactGrant.grant_id == grant_id).delete(synchronize_session=False)
        db.query(ContactAccessAudit).filter(ContactAccessAudit.request_id == request_id).delete(synchronize_session=False)
        db.query(ContactRequest).filter(ContactRequest.request_id == request_id).delete(synchronize_session=False)
        if event_id is not None:
            db.query(WecomInboundEvent).filter(WecomInboundEvent.id == event_id).delete(synchronize_session=False)
        db.commit()
        db.close()
