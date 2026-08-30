from pathlib import Path


def test_action_reference_migration_repairs_each_column_independently():
    migration = Path(__file__).parents[2] / "sql" / "migrations" / "phase13_001_action_result_reference.sql"
    sql = migration.read_text(encoding="utf-8")
    repair = sql.split("-- Repair guard:", 1)[1]
    required = {
        "actor_userid", "action_version", "result_ref_type", "request_id", "snapshot_id",
        "delivery_ids", "outbox_ids", "session_commit_id", "result_schema_version",
        "failure_code", "replay_count", "last_replayed_at", "parse_ref", "parse_digest",
        "parse_version", "parse_expires_at",
    }
    for column in required:
        assert f"column_name='{column}'" in repair
        assert f"ADD COLUMN `{column}`" in repair
