from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_backfill_and_verify_are_bounded_and_non_destructive():
    backfill = (ROOT / "scripts" / "contact_pii_backfill.py").read_text(encoding="utf-8")
    verify = (ROOT / "scripts" / "contact_pii_verify.py").read_text(encoding="utf-8")
    assert "batch_size" in backfill
    assert "status='paused'" in backfill
    assert "db.commit()" in backfill
    assert "DROP COLUMN" not in backfill.upper()
    assert "DROP COLUMN" not in verify.upper()


def test_contact_dtos_reject_plaintext_contact_fields():
    schema = (ROOT / "app" / "schemas" / "contact.py").read_text(encoding="utf-8")
    assert "phone:" not in schema
    assert "wechat:" not in schema
    assert "contact_person:" not in schema
