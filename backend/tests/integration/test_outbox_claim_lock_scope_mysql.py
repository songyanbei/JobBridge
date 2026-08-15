"""Real MySQL evidence that outbox claims lock only outbox rows."""
from __future__ import annotations

import threading
from uuid import uuid4

import pytest

from app.db import SessionLocal
from app.models import WecomInboundEvent, WecomOutboundOutbox
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
