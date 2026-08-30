from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.listing.contact import (
    assert_listing_version_current,
    contact_reference,
    is_listing_version_current,
)
from app.services.job_lifecycle_service import contact_version_is_current


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


def test_phase14_media_migration_is_additive():
    migration = Path(__file__).parents[2] / "sql" / "migrations" / "phase14_002_job_media_version.sql"
    text = migration.read_text(encoding="utf-8")
    assert "ADD COLUMN `entity_version`" in text
    assert "idx_media_entity_version" in text
    schema = migration.parents[1] / "schema.sql"
    schema_text = schema.read_text(encoding="utf-8")
    assert "`entity_version` INT UNSIGNED" in schema_text
    assert "idx_media_entity_version" in schema_text
