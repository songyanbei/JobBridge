from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_contact_paths_are_fail_closed_and_pii_free():
    contact = (ROOT / "app" / "listing" / "contact.py").read_text(encoding="utf-8")
    assert "contact_service_off" in contact
    assert "ContactDelivery" in contact
    assert "legacy phone" in contact
    assert "token_hash" in contact


def test_freeze_gate_requires_completed_entities_and_uses_triggers():
    source = (ROOT / "scripts" / "contact_pii_freeze.py").read_text(encoding="utf-8")
    assert "migration_incomplete" in source
    assert "CREATE TRIGGER" in source
    assert "contact legacy PII columns are read-only" in source
