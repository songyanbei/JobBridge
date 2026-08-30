from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_dispatcher_is_fail_closed_without_ciphertext():
    source = (ROOT / "app" / "listing" / "contact.py").read_text(encoding="utf-8")
    assert "delivery_ciphertext_missing" in source
    assert "crypto_unavailable" in source
    assert "delivery_decrypt_failed" in source
    assert "delivery.status = \"retry_wait\"" in source
    assert "outbox.status = \"pending\"" in source
    assert "outbox.status, outbox.sent_at = \"sent\", delivery.sent_at" in source
    assert "provider_msg_id" in source


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


def test_contact_delivery_migration_uses_mysql_safe_idempotent_kind_triggers():
    migration = (ROOT / "sql" / "migrations" / "phase13_013_contact_delivery.sql").read_text(encoding="utf-8")
    lowered = migration.lower()
    # A CHECK over recommendation_delivery_id is rejected by MySQL 8 when
    # that column has a referential action (ERROR 3823).
    assert "check (not (recommendation_delivery_id" not in lowered
    assert "trg_outbox_single_delivery_kind_ins" in lowered
    assert "trg_outbox_single_delivery_kind_upd" in lowered
    assert "before insert" in lowered
    assert "before update" in lowered
    assert "signal sqlstate ''45000''" in lowered
    # Both trigger creation and the optional legacy CHECK drop are guarded by
    # information_schema lookups, so a run interrupted after ALTER succeeds.
    assert "information_schema.triggers" in lowered
    assert "information_schema.table_constraints" in lowered
    assert "drop check `ck_outbox_single_delivery_kind`" in lowered
