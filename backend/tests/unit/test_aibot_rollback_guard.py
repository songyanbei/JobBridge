from pathlib import Path


ROLLBACK_SQL = (
    Path(__file__).parents[2]
    / "sql"
    / "migrations"
    / "phase14_001_aibot_channel_down.sql"
).read_text(encoding="utf-8")


def test_aibot_rollback_executes_guards_before_destructive_operations():
    guard_call = ROLLBACK_SQL.index("CALL phase14_assert_aibot_rollback_guards();")
    first_drop = ROLLBACK_SQL.index("CALL phase14_drop_check_if_exists")
    assert guard_call < first_drop
    assert "wecom_aibot.rollout_enabled" in ROLLBACK_SQL
    assert "aibot rollout is not disabled" in ROLLBACK_SQL
    assert "aibot data cleanup is incomplete" in ROLLBACK_SQL
    assert "audit confirmation is missing" in ROLLBACK_SQL
    assert "SIGNAL SQLSTATE '45000'" in ROLLBACK_SQL


def test_aibot_rollback_guard_requires_empty_channel_rows_and_audit_operator():
    guard = ROLLBACK_SQL[ROLLBACK_SQL.index("CREATE PROCEDURE phase14_assert_aibot_rollback_guards") :]
    guard = guard[:guard.index("CREATE PROCEDURE phase14_drop_index_if_exists")]
    assert "WHERE source_channel = 'wecom_aibot'" in guard
    assert "WHERE channel = 'wecom_aibot'" in guard
    assert "FROM wecom_aibot_identity" in guard
    assert "target_type = 'system'" in guard
    assert "action = 'strategy_rollback'" in guard
    assert "operator IS NOT NULL" in guard
