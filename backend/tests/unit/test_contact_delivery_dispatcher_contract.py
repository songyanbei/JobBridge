from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_dispatcher_is_fail_closed_without_ciphertext():
    source = (ROOT / "app" / "listing" / "contact.py").read_text(encoding="utf-8")
    assert "delivery_ciphertext_missing" in source
    assert "crypto_unavailable" in source
    assert "delivery_decrypt_failed" in source
    assert "delivery.status = \"retry_wait\"" in source


def test_contact_delivery_has_no_outbox_plaintext_path():
    service = (ROOT / "app" / "listing" / "contact.py").read_text(encoding="utf-8")
    model = (ROOT / "app" / "models.py").read_text(encoding="utf-8")
    migration = (ROOT / "sql" / "migrations" / "phase13_013_contact_delivery.sql").read_text(encoding="utf-8")
    assert "contact_delivery_id" in model
    assert "WecomOutboundOutbox(" in service
    assert "content=None" in service
    assert "content_ciphertext" in migration
    assert "grant_id" in migration
    # The outbox stores only the opaque reference; no contact value column is added.
    assert "phone" not in migration.lower()
    assert "wechat" not in migration.lower()
