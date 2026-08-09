from contextlib import nullcontext
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateTable

from app.models import MediaAssetLifecycle
from app.services.job_media_service import mark_delete_pending
from app.tasks import media_cleanup_worker


@compiles(mysql.TINYINT, "sqlite")
@compiles(mysql.SMALLINT, "sqlite")
@compiles(mysql.INTEGER, "sqlite")
@compiles(mysql.BIGINT, "sqlite")
def _compile_mysql_integer_for_sqlite(_type, _compiler, **_kwargs):
    return "INTEGER"


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(CreateTable(MediaAssetLifecycle.__table__))
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _media(object_key: str, state: str, **values) -> MediaAssetLifecycle:
    defaults = {
        "owner_userid": "owner-1",
        "entity_type": "resume",
        "entity_id": 7,
        "attempt_count": 0,
    }
    defaults.update(values)
    return MediaAssetLifecycle(object_key=object_key, state=state, **defaults)


def test_claim_excludes_dead_letter_and_future_retry(db):
    now = datetime.utcnow()
    due = _media(
        "images/due.jpg",
        "delete_pending",
        next_attempt_at=now - timedelta(seconds=1),
    )
    dead = _media(
        "images/dead.jpg",
        "dead_letter",
        next_attempt_at=now - timedelta(seconds=1),
        attempt_count=10,
    )
    future = _media(
        "images/future.jpg",
        "delete_pending",
        next_attempt_at=now + timedelta(hours=1),
    )
    expired_draft = _media(
        "images/draft.jpg",
        "pending",
        entity_type=None,
        entity_id=None,
        draft_expires_at=now - timedelta(seconds=1),
    )
    db.add_all([due, dead, future, expired_draft])
    db.commit()

    claimed = media_cleanup_worker._claim_ids(db, "worker-1", now, 100)

    assert set(claimed) == {due.id, expired_draft.id}
    db.refresh(dead)
    db.refresh(future)
    assert dead.state == "dead_letter" and dead.lease_owner is None
    assert future.state == "delete_pending" and future.lease_owner is None


def _claimed_row(*, attempts: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=17,
        entity_type="resume",
        entity_id=9,
        state="delete_pending",
        attempt_count=attempts,
        last_error=None,
        next_attempt_at=None,
        lease_owner="worker-1",
        lease_expires_at=datetime.utcnow() + timedelta(minutes=1),
        deleted_at=None,
    )


def test_delete_failure_retries_with_backoff_before_threshold():
    row = _claimed_row(attempts=8)
    now = datetime.utcnow()

    outcome = media_cleanup_worker._apply_delete_result(
        row,
        error=RuntimeError("storage unavailable"),
        now=now,
    )

    assert outcome == "retry_wait"
    assert row.state == "delete_pending"
    assert row.attempt_count == 9
    assert row.next_attempt_at == now + timedelta(seconds=512)
    assert row.last_error == "storage unavailable"
    assert row.lease_owner is None and row.lease_expires_at is None


def test_tenth_delete_failure_enters_dead_letter_and_alerts():
    row = _claimed_row(attempts=9)
    now = datetime.utcnow()

    with patch.object(media_cleanup_worker, "log_event") as log:
        outcome = media_cleanup_worker._apply_delete_result(
            row,
            error=RuntimeError("still unavailable"),
            now=now,
        )

    assert outcome == "dead_letter"
    assert row.state == "dead_letter"
    assert row.attempt_count == 10
    assert row.next_attempt_at is None
    assert row.deleted_at is None
    assert row.lease_owner is None and row.lease_expires_at is None
    log.assert_called_once_with(
        "media_cleanup_dead_lettered",
        media_id=17,
        entity_type="resume",
        entity_id=9,
        attempt_count=10,
        severity="alert",
    )


def test_success_clears_retry_state_and_marks_deleted():
    row = _claimed_row(attempts=3)
    row.last_error = "old failure"
    row.next_attempt_at = datetime.utcnow()
    now = datetime.utcnow()

    outcome = media_cleanup_worker._apply_delete_result(row, error=None, now=now)

    assert outcome == "deleted"
    assert row.state == "deleted"
    assert row.deleted_at == now
    assert row.last_error is None and row.next_attempt_at is None
    assert row.lease_owner is None and row.lease_expires_at is None


def _run_with_storage(monkeypatch, row, storage_factory):
    claim_db = MagicMock()
    lookup_db = MagicMock()
    finish_db = MagicMock()
    sessions = iter([
        nullcontext(claim_db),
        nullcontext(lookup_db),
        nullcontext(finish_db),
    ])
    seen_errors = []

    def _finish(_db, _media_id, _owner, *, error, now):
        seen_errors.append(error)
        return media_cleanup_worker._apply_delete_result(row, error=error, now=now)

    monkeypatch.setattr(media_cleanup_worker, "task_lock", lambda *_args, **_kwargs: nullcontext(True))
    monkeypatch.setattr(media_cleanup_worker, "SessionLocal", lambda: next(sessions))
    monkeypatch.setattr(media_cleanup_worker, "_claim_ids", lambda *_args: [17])
    monkeypatch.setattr(
        media_cleanup_worker,
        "_renew_claimed_object_key",
        lambda *_args: row.object_key,
    )
    monkeypatch.setattr(media_cleanup_worker, "_finish_claimed_result", _finish)
    monkeypatch.setattr(media_cleanup_worker, "get_storage", storage_factory)

    media_cleanup_worker.run()
    return seen_errors


def test_storage_not_found_true_is_idempotent_success(monkeypatch):
    row = _claimed_row(attempts=1)
    row.object_key = "images/already-gone.jpg"
    storage = MagicMock()
    storage.delete.return_value = True

    seen_errors = _run_with_storage(monkeypatch, row, lambda: storage)

    storage.delete.assert_called_once_with("images/already-gone.jpg")
    assert seen_errors == [None]
    assert row.state == "deleted"


def test_storage_false_is_retried_instead_of_marked_deleted(monkeypatch):
    row = _claimed_row(attempts=1)
    row.object_key = "images/unconfirmed.jpg"
    storage = MagicMock()
    storage.delete.return_value = False

    seen_errors = _run_with_storage(monkeypatch, row, lambda: storage)

    assert len(seen_errors) == 1
    assert str(seen_errors[0]) == "storage delete was not confirmed"
    assert row.state == "delete_pending"
    assert row.attempt_count == 2
    assert row.deleted_at is None


def test_storage_initialization_failure_is_persisted_for_every_claim(monkeypatch):
    row = _claimed_row(attempts=4)
    row.object_key = "images/provider-unavailable.jpg"

    def _raise_storage_error():
        raise RuntimeError("storage provider unavailable")

    seen_errors = _run_with_storage(monkeypatch, row, _raise_storage_error)

    assert len(seen_errors) == 1
    assert str(seen_errors[0]) == "storage provider unavailable"
    assert row.state == "delete_pending"
    assert row.attempt_count == 5
    assert row.lease_owner is None and row.lease_expires_at is None


@pytest.mark.parametrize(
    "lease_owner,lease_expires_at",
    [
        ("other-worker", datetime.utcnow() + timedelta(minutes=1)),
        ("worker-1", datetime.utcnow() - timedelta(seconds=1)),
    ],
)
def test_renew_rejects_stolen_or_expired_lease(db, lease_owner, lease_expires_at):
    row = _media(
        "images/fenced-before-delete.jpg",
        "delete_pending",
        lease_owner=lease_owner,
        lease_expires_at=lease_expires_at,
    )
    db.add(row)
    db.commit()

    result = media_cleanup_worker._renew_claimed_object_key(
        db, row.id, "worker-1", datetime.utcnow()
    )

    assert result is None
    db.refresh(row)
    assert row.state == "delete_pending"


@pytest.mark.parametrize(
    "lease_owner,lease_expires_at",
    [
        ("other-worker", datetime.utcnow() + timedelta(minutes=1)),
        ("worker-1", datetime.utcnow() - timedelta(seconds=1)),
    ],
)
def test_finish_rejects_stolen_or_expired_lease(db, lease_owner, lease_expires_at):
    row = _media(
        "images/fenced-after-delete.jpg",
        "delete_pending",
        lease_owner=lease_owner,
        lease_expires_at=lease_expires_at,
    )
    db.add(row)
    db.commit()

    result = media_cleanup_worker._finish_claimed_result(
        db,
        row.id,
        "worker-1",
        error=None,
        now=datetime.utcnow(),
    )

    assert result is None
    db.refresh(row)
    assert row.state == "delete_pending"
    assert row.deleted_at is None


def test_terminal_media_is_not_requeued_by_repeated_cleanup(db):
    dead = _media("images/dead-terminal.jpg", "dead_letter", attempt_count=10)
    deleted = _media("images/deleted-terminal.jpg", "deleted")
    retry_at = datetime.utcnow() + timedelta(hours=1)
    retry = _media(
        "images/retry-backoff.jpg",
        "delete_pending",
        attempt_count=3,
        next_attempt_at=retry_at,
    )
    db.add_all([dead, deleted, retry])
    db.commit()

    mark_delete_pending(db, [dead.id, deleted.id, retry.id])
    db.commit()

    db.refresh(dead)
    db.refresh(deleted)
    db.refresh(retry)
    assert dead.state == "dead_letter"
    assert deleted.state == "deleted"
    assert retry.state == "delete_pending"
    assert retry.next_attempt_at == retry_at
