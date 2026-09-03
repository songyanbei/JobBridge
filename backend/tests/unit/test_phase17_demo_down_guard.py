from pathlib import Path


def test_phase17_down_migration_guards_demo_runtime_before_drop():
    sql = (Path(__file__).parents[2] / "sql" / "migrations" / "phase17_down_001_demo_control_plane.sql").read_text(
        encoding="utf-8"
    ).lower()
    normalized = sql.replace(chr(96), "")

    assert "create procedure" in normalized
    assert "signal sqlstate '45000'" in normalized
    assert "status <> 'cleaned'" in normalized
    assert "membership_status = 'active'" in normalized
    assert "lifecycle_status <> 'cleaned'" in normalized
    assert "channel = 'wecom_aibot'" in normalized
    assert "status in ('pending', 'sending')" in normalized
    assert "status in ('received', 'processing', 'session_pending')" in normalized
    assert "call phase17_assert_demo_down_guards()" in normalized
    assert normalized.index("call phase17_assert_demo_down_guards()") < normalized.index("drop table if exists demo_resource")
