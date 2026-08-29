"""Action execution idempotency, lease and fencing contracts."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.models import ActionExecution
from app.services.action_execution_service import (
    ActionExecutionConflict,
    claim_action_execution,
    finalize_action_execution,
    read_action_execution,
)


@compiles(mysql.TINYINT, "sqlite")
@compiles(mysql.SMALLINT, "sqlite")
@compiles(mysql.INTEGER, "sqlite")
@compiles(mysql.BIGINT, "sqlite")
def _compile_mysql_integer_for_sqlite(_type, _compiler, **_kwargs):
    return "INTEGER"


@compiles(mysql.DATETIME, "sqlite")
def _compile_mysql_datetime_for_sqlite(_type, _compiler, **_kwargs):
    return "DATETIME"


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        # The production model uses MySQL's CURRENT_TIMESTAMP(6), which
        # SQLite rejects in a CREATE TABLE default.  Keep the same columns
        # and constraints while using SQLite's portable timestamp default.
        connection.exec_driver_sql(
            """
            CREATE TABLE action_execution (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                turn_id VARCHAR(36) NOT NULL,
                action_name VARCHAR(64) NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'started',
                request_digest CHAR(64),
                result_digest CHAR(64),
                lease_owner VARCHAR(64),
                lease_until DATETIME,
                fencing_token INTEGER NOT NULL DEFAULT 1,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at DATETIME,
                CONSTRAINT uk_action_execution_turn_action
                    UNIQUE (turn_id, action_name)
            )
            """
        )
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _at(second: int = 0) -> datetime:
    return datetime(2026, 8, 29, 12, 0, 0) + timedelta(seconds=second)


def test_turn_action_unique_key_and_live_claim_is_not_stolen(db):
    first = claim_action_execution(
        db, "turn-1", "listing.search", "worker-a",
        request_digest="a" * 64, lease_seconds=30, now=_at(),
    )
    db.commit()

    assert first.state == "acquired"
    assert first.fencing_token == 1
    assert first.acquired is True

    second = claim_action_execution(
        db, "turn-1", "listing.search", "worker-b",
        request_digest="a" * 64, lease_seconds=30, now=_at(1),
    )
    assert second.state == "in_progress"
    assert second.busy is True
    assert second.fencing_token == first.fencing_token
    assert second.row.lease_owner == "worker-a"

    saved = read_action_execution(db, "turn-1", "listing.search")
    assert saved is not None
    assert saved.request_digest == "a" * 64


def test_expired_started_claim_is_reclaimed_with_incremented_fence(db):
    first = claim_action_execution(
        db, "turn-2", "listing.show_more", "worker-a",
        lease_seconds=10, now=_at(),
    )
    db.commit()

    takeover = claim_action_execution(
        db, "turn-2", "listing.show_more", "worker-b",
        lease_seconds=10, now=_at(11),
    )
    assert takeover.state == "acquired"
    assert takeover.fencing_token == first.fencing_token + 1
    assert takeover.row.lease_owner == "worker-b"
    assert takeover.row.lease_until == _at(21)

    # A stale worker cannot finalize after takeover, even with the same key.
    assert not finalize_action_execution(
        db, "turn-2", "listing.show_more", "worker-a", first.fencing_token,
        result_digest="old", now=_at(12),
    )
    assert finalize_action_execution(
        db, "turn-2", "listing.show_more", "worker-b", takeover.fencing_token,
        result_digest="new", now=_at(12),
    )
    db.commit()
    row = read_action_execution(db, "turn-2", "listing.show_more")
    assert row.status == "succeeded"
    assert row.result_digest == "new"


def test_succeeded_retry_replays_saved_result_without_reclaim(db):
    claim = claim_action_execution(
        db, "turn-3", "listing.search", "worker-a", lease_seconds=30, now=_at(),
    )
    assert finalize_action_execution(
        db, "turn-3", "listing.search", "worker-a", claim.fencing_token,
        result_digest="snapshot-digest", now=_at(1),
    )
    db.commit()

    retry = claim_action_execution(
        db, "turn-3", "listing.search", "worker-b", lease_seconds=30, now=_at(2),
    )
    assert retry.state == "succeeded"
    assert retry.replay is True
    assert retry.result_digest == "snapshot-digest"
    assert retry.fencing_token == claim.fencing_token
    assert retry.row.lease_owner is None


def test_retryable_failure_reclaims_but_terminal_failure_does_not(db):
    retryable = claim_action_execution(
        db, "turn-4", "listing.relax_search", "worker-a", lease_seconds=30, now=_at(),
    )
    assert finalize_action_execution(
        db, "turn-4", "listing.relax_search", "worker-a", retryable.fencing_token,
        status="failed_retryable", result_digest=None, now=_at(1),
    )
    db.commit()

    retried = claim_action_execution(
        db, "turn-4", "listing.relax_search", "worker-b", lease_seconds=30, now=_at(2),
    )
    assert retried.state == "acquired"
    assert retried.fencing_token == retryable.fencing_token + 1

    terminal = claim_action_execution(
        db, "turn-5", "listing.search", "worker-a", lease_seconds=30, now=_at(),
    )
    assert finalize_action_execution(
        db, "turn-5", "listing.search", "worker-a", terminal.fencing_token,
        status="failed_terminal", now=_at(1),
    )
    db.commit()
    blocked = claim_action_execution(
        db, "turn-5", "listing.search", "worker-b", lease_seconds=30, now=_at(2),
    )
    assert blocked.state == "failed_terminal"
    assert blocked.acquired is False


def test_request_digest_is_part_of_idempotency_contract(db):
    claim_action_execution(
        db, "turn-6", "listing.search", "worker-a",
        request_digest="a" * 64, lease_seconds=30, now=_at(),
    )
    db.commit()
    with pytest.raises(ActionExecutionConflict, match="request_digest_mismatch"):
        claim_action_execution(
            db, "turn-6", "listing.search", "worker-b",
            request_digest="b" * 64, lease_seconds=30, now=_at(1),
        )


def test_finalize_requires_current_owner_and_live_fence(db):
    claim = claim_action_execution(
        db, "turn-7", "listing.search", "worker-a", lease_seconds=5, now=_at(),
    )
    assert not finalize_action_execution(
        db, "turn-7", "listing.search", "worker-b", claim.fencing_token,
        now=_at(1),
    )
    assert not finalize_action_execution(
        db, "turn-7", "listing.search", "worker-a", claim.fencing_token + 1,
        now=_at(1),
    )
    # The owner/token pair is also rejected once the lease has expired.
    assert not finalize_action_execution(
        db, "turn-7", "listing.search", "worker-a", claim.fencing_token,
        now=_at(6),
    )
    row = read_action_execution(db, "turn-7", "listing.search")
    assert row.status == "started"
