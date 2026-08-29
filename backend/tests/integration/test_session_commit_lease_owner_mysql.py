"""MySQL fencing for durable session commit claims."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.db import SessionLocal
from app.models import WecomInboundEvent
from app.services.worker import SESSION_COMMIT_STALE_SECONDS, Worker


pytestmark = pytest.mark.integration


def _apply_lease_owner_migration(db) -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "sql/migrations/phase10_004_session_commit_lease_owner.sql"
    )
    for statement in path.read_text(encoding="utf-8").split(";"):
        if statement.strip():
            db.execute(text(statement))
    db.commit()


def test_session_claim_owner_schema_and_stale_owner_fencing():
    setup_db = SessionLocal()
    event_id = None
    try:
        _apply_lease_owner_migration(setup_db)
        _apply_lease_owner_migration(setup_db)
        column = setup_db.execute(text(
            "SELECT column_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema=DATABASE() "
            "AND table_name='wecom_inbound_event' "
            "AND column_name='session_apply_lease_owner'"
        )).one()
        assert column[0] == "varchar(64)"
        assert column[1] == "YES"

        row = WecomInboundEvent(
            msg_id=f"session-lease-{uuid4()}",
            from_userid="integration-session-lease",
            msg_type="text",
            content_brief="session lease",
            status="session_pending",
            session_operation="delete",
            session_expected_version=0,
            session_payload={},
            session_commit_deadline_epoch=Decimal("9999999999.000000"),
        )
        setup_db.add(row)
        setup_db.commit()
        event_id = int(row.id)

        first_worker = object.__new__(Worker)
        first_claim = first_worker._claim_session_commits(
            inbound_event_id=event_id, limit=1,
        )[0]
        first_owner = first_claim["lease_owner"]
        assert len(first_owner) <= 64

        takeover_db = SessionLocal()
        try:
            takeover_db.execute(text(
                "UPDATE wecom_inbound_event "
                "SET session_apply_locked_at="
                "TIMESTAMPADD(SECOND, :age, NOW(6)) "
                "WHERE id=:event_id"
            ), {
                "age": -(SESSION_COMMIT_STALE_SECONDS + 1),
                "event_id": event_id,
            })
            takeover_db.commit()
        finally:
            takeover_db.close()

        assert first_worker._mark_session_commit_retry(
            first_claim, RuntimeError("expired owner"),
        ) is False

        second_worker = object.__new__(Worker)
        second_claim = second_worker._claim_session_commits(
            inbound_event_id=event_id, limit=1,
        )[0]
        assert second_claim["lease_owner"] != first_owner
        assert first_worker._mark_session_commit_applied(
            event_id, first_owner,
        ) is False

        verify_db = SessionLocal()
        try:
            current = verify_db.get(WecomInboundEvent, event_id)
            assert current.status == "session_pending"
            assert current.session_apply_lease_owner == second_claim["lease_owner"]
        finally:
            verify_db.close()

        assert second_worker._mark_session_commit_applied(
            event_id, second_claim["lease_owner"],
        ) is True
        setup_db.expire_all()
        assert setup_db.get(WecomInboundEvent, event_id).status == "done"
    finally:
        setup_db.rollback()
        if event_id is not None:
            setup_db.query(WecomInboundEvent).filter(
                WecomInboundEvent.id == event_id,
            ).delete(synchronize_session=False)
            setup_db.commit()
        setup_db.close()
