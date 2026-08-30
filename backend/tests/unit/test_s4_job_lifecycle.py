from datetime import datetime, timedelta
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.listing.contact import (
    assert_listing_version_current,
    contact_reference,
    is_listing_version_current,
)
from app.services.job_lifecycle_service import _emit, contact_version_is_current, transition_job


def _job(**overrides):
    values = dict(
        version=3,
        audit_status="passed",
        deleted_at=None,
        delist_reason=None,
        expires_at=datetime.utcnow() + timedelta(hours=1),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_contact_grant_is_bound_to_live_job_version():
    job = _job()
    assert contact_version_is_current(job, 3)
    assert is_listing_version_current(job, 3)
    assert not is_listing_version_current(job, 2)
    assert not is_listing_version_current(_job(delist_reason="manual_delist"), 3)
    with pytest.raises(ValueError, match="contact_listing_version_invalid"):
        assert_listing_version_current(job, 2)


def test_contact_reference_is_opaque_and_typed():
    assert contact_reference("job", 8, 3) == {
        "listing_type": "job", "listing_id": 8, "listing_version": 3,
    }
    with pytest.raises(ValueError):
        contact_reference("user", 8, 3)


def test_transition_emits_versioned_domain_event(monkeypatch):
    append_domain_event = MagicMock()
    monkeypatch.setitem(
        sys.modules,
        "app.services.domain_outbox_service",
        SimpleNamespace(append_domain_event=append_domain_event),
    )
    job = _job(delist_reason="manual_delist")
    job.id = 8
    job.aggregate_version = 11
    db = MagicMock()
    db.info = {}
    db.query.return_value.populate_existing.return_value.filter.return_value.with_for_update.return_value.one_or_none.return_value = job

    transition_job(db, 8, action="restore", reason="operator_restore")

    append_domain_event.assert_called_once_with(
        db,
        aggregate_type="job",
        aggregate_id=8,
        aggregate_version=12,
        event_type="job.restored",
        payload={"reason": "operator_restore"},
        tombstone=False,
    )


@pytest.mark.parametrize(
    ("action", "overrides", "event_type"),
    [
        ("delist", {}, "job.delisted"),
        ("expire", {"expires_at": datetime.utcnow() - timedelta(hours=1)}, "job.expired"),
        ("restore", {"delist_reason": "manual_delist"}, "job.restored"),
        ("replace", {}, "job.replaced"),
    ],
)
def test_transition_bumps_aggregate_version_for_every_mutation(monkeypatch, action, overrides, event_type):
    append_domain_event = MagicMock()
    monkeypatch.setitem(sys.modules, "app.services.domain_outbox_service", SimpleNamespace(append_domain_event=append_domain_event))
    values = {"id": 9, "version": 3, "aggregate_version": 10, "audit_status": "passed", "deleted_at": None, "delist_reason": None, "expires_at": datetime.utcnow() + timedelta(hours=1)}
    values.update(overrides)
    job = SimpleNamespace(**values)
    db = MagicMock(); db.info = {}
    db.query.return_value.populate_existing.return_value.filter.return_value.with_for_update.return_value.one_or_none.return_value = job
    transition_reason = "manual_delist" if action == "delist" else "test"
    transition_job(db, 9, action=action, reason=transition_reason)
    assert job.version == 4
    assert job.aggregate_version == 11
    assert append_domain_event.call_args.kwargs["aggregate_version"] == 11
    assert append_domain_event.call_args.kwargs["event_type"] == event_type


def test_emit_missing_domain_outbox_table_keeps_legacy_pending_event(monkeypatch):
    append_domain_event = MagicMock()
    monkeypatch.setitem(sys.modules, "app.services.domain_outbox_service", SimpleNamespace(append_domain_event=append_domain_event))
    monkeypatch.setattr("app.services.job_lifecycle_service.inspect", lambda _: SimpleNamespace(has_table=lambda _: False))
    db = MagicMock(); db.info = {}; db.bind = SimpleNamespace(dialect=SimpleNamespace(name="mysql"))
    job = _job(id=10, aggregate_version=4)
    _emit(db, job, "job.delisted", reason="manual_delist", tombstone=True)
    append_domain_event.assert_not_called()
    assert db.info["pending_job_lifecycle_events"][0]["aggregate_version"] == 4


def test_phase14_media_migration_is_additive():
    migration = Path(__file__).parents[2] / "sql" / "migrations" / "phase14_002_job_media_version.sql"
    text = migration.read_text(encoding="utf-8")
    assert "ADD COLUMN `entity_version`" in text
    assert "idx_media_entity_version" in text
    schema = migration.parents[1] / "schema.sql"
    schema_text = schema.read_text(encoding="utf-8")
    assert "`entity_version` INT UNSIGNED" in schema_text
    assert "idx_media_entity_version" in schema_text
    assert (migration.parent / "phase14_down_002_job_media_version.sql").exists()
    lifecycle = migration.parent / "phase14_003_job_lifecycle_event.sql"
    assert lifecycle.exists()
    assert "idx_job_lifecycle_version" in lifecycle.read_text(encoding="utf-8")
