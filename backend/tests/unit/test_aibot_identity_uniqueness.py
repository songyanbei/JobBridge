from pathlib import Path

from app.models import AibotIdentityBinding, AibotRegistration


def test_binding_has_canonical_active_uniqueness_constraint():
    constraints = {
        c.name: tuple(col.name for col in c.columns)
        for c in AibotIdentityBinding.__table__.constraints
        if c.name
    }
    assert constraints["uk_aibot_binding_canonical_status"] == (
        "bot_id", "canonical_userid", "binding_status",
    )


def test_registration_has_one_row_per_binding_constraint():
    constraints = {
        c.name: tuple(col.name for col in c.columns)
        for c in AibotRegistration.__table__.constraints
        if c.name
    }
    assert constraints["uk_aibot_registration_binding"] == ("identity_binding_id",)


def test_phase16_migration_adds_unique_indexes_without_data_deletion():
    path = Path(__file__).parents[2] / "sql" / "migrations" / "phase16_001_aibot_identity_role_binding.sql"
    sql = path.read_text(encoding="utf-8").lower()
    assert "uk_aibot_binding_canonical_status" in sql
    assert "uk_aibot_registration_binding" in sql
    assert "delete from" not in sql
    assert "duplicate preflight" in sql

