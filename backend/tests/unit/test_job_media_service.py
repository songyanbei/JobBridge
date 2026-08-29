import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.config import settings
from app.services.job_media_service import (
    discard_pending_media,
    hard_delete_media_complete,
    mark_entity_media_delete_pending,
    resume_hard_delete_media_complete,
)
from app.services.storage_reference_service import (
    normalize_storage_reference,
    storage_urls_for_response,
)


def test_normalizes_raw_key_and_local_url():
    assert normalize_storage_reference("images/user/a.jpg") == "images/user/a.jpg"
    assert normalize_storage_reference("/files/images/user/a.jpg") == "images/user/a.jpg"


def test_normalizes_trusted_signed_url(monkeypatch):
    monkeypatch.setattr(settings, "oss_trusted_origins", "https://assets.example.com")
    assert normalize_storage_reference(
        "https://assets.example.com/files/images/a.jpg?signature=secret#preview"
    ) == "images/a.jpg"


def test_response_urls_are_generated_without_persisting_access_paths(monkeypatch):
    storage = MagicMock()
    storage.get_url.side_effect = lambda key: f"/files/{key}"
    monkeypatch.setattr("app.storage.get_storage", lambda: storage)

    assert storage_urls_for_response([
        "images/user/a.jpg", "/files/images/user/b.jpg",
    ]) == [
        "/files/images/user/a.jpg", "/files/images/user/b.jpg",
    ]
    assert [call.args[0] for call in storage.get_url.call_args_list] == [
        "images/user/a.jpg", "images/user/b.jpg",
    ]


def test_response_urls_omit_unresolved_legacy_references(monkeypatch):
    storage = MagicMock()
    storage.get_url.side_effect = lambda key: f"/files/{key}"
    monkeypatch.setattr("app.storage.get_storage", lambda: storage)
    monkeypatch.setattr(settings, "oss_trusted_origins", "")

    assert storage_urls_for_response([
        "images/user/a.jpg", "https://external.example/a.jpg",
    ]) == ["/files/images/user/a.jpg"]


@pytest.mark.parametrize("value", [
    "../secret.jpg",
    "images/../secret.jpg",
    "C:/secret.jpg",
    "images\\secret.jpg",
    "images/%ZZ.jpg",
    "images/%252e%252e/secret.jpg",
    "https://evil.example/files/images/a.jpg?signature=secret",
    "/other/images/a.jpg",
])
def test_rejects_unsafe_or_untrusted_references(value):
    with pytest.raises(ValueError):
        normalize_storage_reference(value)


def _media_db(rows):
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = rows
    return db


def test_hard_delete_media_requires_exact_deleted_key_coverage():
    rows = [
        SimpleNamespace(object_key="images/a.jpg", state="deleted"),
        SimpleNamespace(object_key="images/b.jpg", state="deleted"),
    ]
    assert hard_delete_media_complete(
        _media_db(rows), 7, ["/files/images/a.jpg", "images/b.jpg"]
    ) is True


@pytest.mark.parametrize("rows", [
    [SimpleNamespace(object_key="images/a.jpg", state="delete_pending")],
    [],
    [
        SimpleNamespace(object_key="images/a.jpg", state="deleted"),
        SimpleNamespace(object_key="images/untracked.jpg", state="delete_pending"),
    ],
])
def test_hard_delete_media_fails_closed_for_incomplete_or_mismatched_rows(rows):
    assert hard_delete_media_complete(_media_db(rows), 7, ["images/a.jpg"]) is False


def test_empty_images_waits_for_any_attached_lifecycle_rows_to_finish():
    pending = [SimpleNamespace(object_key="images/orphan.jpg", state="delete_pending")]
    deleted = [SimpleNamespace(object_key="images/orphan.jpg", state="deleted")]
    assert hard_delete_media_complete(_media_db(pending), 7, []) is False
    assert hard_delete_media_complete(_media_db(deleted), 7, []) is True


def test_deleted_historical_alias_row_does_not_block_complete_coverage():
    rows = [
        SimpleNamespace(object_key="images/a.jpg", state="deleted"),
        SimpleNamespace(object_key="images/old-alias.jpg", state="deleted"),
    ]
    assert hard_delete_media_complete(_media_db(rows), 7, ["images/a.jpg"]) is True


def test_resume_hard_delete_uses_the_same_fail_closed_media_gate():
    deleted = [SimpleNamespace(object_key="images/resume/a.jpg", state="deleted")]
    dead_letter = [
        SimpleNamespace(object_key="images/resume/a.jpg", state="dead_letter")
    ]

    assert resume_hard_delete_media_complete(
        _media_db(deleted), 9, ["images/resume/a.jpg"]
    ) is True
    assert resume_hard_delete_media_complete(
        _media_db(dead_letter), 9, ["images/resume/a.jpg"]
    ) is False


def test_entity_cleanup_locks_media_in_id_order_before_transitioning():
    first = SimpleNamespace(id=3, state="attached", next_attempt_at=None)
    second = SimpleNamespace(id=7, state="attached", next_attempt_at=None)
    db = MagicMock()
    query = db.query.return_value
    locked = (
        query.populate_existing.return_value.filter.return_value
        .order_by.return_value.with_for_update.return_value
    )
    locked.all.return_value = [first, second]

    assert mark_entity_media_delete_pending(db, "job", 42) == 2

    query.populate_existing.return_value.filter.return_value.order_by.assert_called_once()
    locked.all.assert_called_once_with()
    assert first.state == second.state == "delete_pending"
    assert first.next_attempt_at is not None
    assert second.next_attempt_at is not None


def test_discard_pending_media_only_transitions_unattached_pending_row():
    row = SimpleNamespace(
        id=12,
        state="pending",
        entity_type=None,
        entity_id=None,
        next_attempt_at=None,
    )
    db = MagicMock()
    locked = (
        db.query.return_value.populate_existing.return_value.filter.return_value
        .with_for_update.return_value
    )
    locked.one_or_none.return_value = row

    assert discard_pending_media(db, 12) is True
    assert row.state == "delete_pending"
    assert row.next_attempt_at is not None


def test_discard_pending_media_does_not_transition_attached_row():
    db = MagicMock()
    locked = (
        db.query.return_value.populate_existing.return_value.filter.return_value
        .with_for_update.return_value
    )
    locked.one_or_none.return_value = None

    assert discard_pending_media(db, 12) is False
