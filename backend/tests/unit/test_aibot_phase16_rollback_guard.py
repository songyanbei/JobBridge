from pathlib import Path


SQL = (
    Path(__file__).parents[2]
    / "sql"
    / "migrations"
    / "phase16_001_aibot_identity_role_binding_down.sql"
).read_text(encoding="utf-8")


def test_phase16_guard_runs_before_any_destructive_operation():
    assert SQL.index("CALL phase16_assert_aibot_rollback_guards();") < SQL.index("DROP TABLE IF EXISTS aibot_registration;")
    assert "SIGNAL SQLSTATE '45000'" in SQL
    assert "migration is incomplete" in SQL
    assert "aibot acceptance is not disabled" in SQL
    assert "identity resolution is not disabled" in SQL


def test_phase16_guard_requires_export_cleanup_and_audit_confirmation():
    assert "export_confirmed" in SQL
    assert "cleanup_confirmed" in SQL
    assert "audit_confirmed" in SQL
    assert "identity data export/cleanup is incomplete" in SQL
    assert "audit_log" in SQL
    assert "drop table `user`" not in SQL.lower()
    assert "wecom_inbound_event" not in SQL
    assert "wecom_outbound_outbox" not in SQL


def test_phase16_down_drops_fk_before_phase16_tables_and_is_repeat_safe():
    assert SQL.index("phase16_drop_fk_if_exists('aibot_identity_binding'") < SQL.index("DROP TABLE IF EXISTS aibot_registration;")
    assert "DROP TABLE IF EXISTS" in SQL
    assert "DROP PROCEDURE IF EXISTS phase16_assert_aibot_rollback_guards" in SQL
