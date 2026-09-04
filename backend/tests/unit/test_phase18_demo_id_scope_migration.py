from pathlib import Path


MIGRATION = (
    Path(__file__).parents[2]
    / "sql"
    / "migrations"
    / "phase18_001_demo_id_scope.sql"
)

DEMO_ID_TABLES = (
    "user",
    "job",
    "resume",
    "conversation_log",
    "audit_log",
    "event_log",
    "wecom_inbound_event",
    "wecom_outbound_outbox",
    "action_execution",
    "action_parse_artifact",
    "contact_request",
    "contact_grant",
    "contact_delivery",
    "contact_access_audit",
    "recommendation_request",
    "recommendation_search_attempt",
    "recommendation_delivery",
    "recommendation_impression",
    "recommendation_exposure_daily",
    "job_replacement",
    "resume_replacement",
    "resume_replacement_rollout_assignment",
    "media_asset_lifecycle",
    "target_cleanup_task",
    "domain_outbox_event",
)

DEMO_ID_INDEXES = (
    ("user", "idx_user_demo"),
    ("job", "idx_job_demo_owner"),
    ("resume", "idx_resume_demo_owner"),
    ("conversation_log", "idx_conversation_demo_time"),
    ("audit_log", "idx_audit_demo_time"),
    ("event_log", "idx_event_demo_time"),
    ("wecom_inbound_event", "idx_inbound_demo_status"),
    ("wecom_outbound_outbox", "idx_outbox_demo_status"),
    ("action_execution", "idx_action_execution_demo"),
    ("action_parse_artifact", "idx_action_parse_demo"),
    ("contact_request", "idx_contact_request_demo"),
    ("contact_grant", "idx_contact_grant_demo"),
    ("contact_delivery", "idx_contact_delivery_demo"),
    ("contact_access_audit", "idx_contact_audit_demo"),
    ("recommendation_request", "idx_recommendation_request_demo_time"),
    ("recommendation_search_attempt", "idx_recommendation_attempt_demo_time"),
    ("recommendation_delivery", "idx_recommendation_delivery_demo_status"),
    ("recommendation_impression", "idx_recommendation_impression_demo_time"),
    ("recommendation_exposure_daily", "idx_recommendation_exposure_demo"),
    ("job_replacement", "idx_replacement_demo_owner"),
    ("resume_replacement", "idx_resume_replacement_demo_owner"),
    ("resume_replacement_rollout_assignment", "idx_resume_rollout_demo_owner"),
    ("media_asset_lifecycle", "idx_media_demo_owner"),
    ("target_cleanup_task", "idx_target_cleanup_demo"),
    ("domain_outbox_event", "idx_domain_outbox_demo"),
)


def test_phase18_uses_mysql8_guarded_column_ddl_for_every_resource():
    sql = MIGRATION.read_text(encoding="utf-8")
    lowered = sql.lower()

    assert "add column if not exists" not in lowered
    assert "set @schema_name = database()" in lowered
    assert lowered.count("prepare phase18_stmt from @ddl") == len(DEMO_ID_TABLES)
    assert lowered.count("prepare stmt from @ddl") == len(DEMO_ID_INDEXES)

    for table in DEMO_ID_TABLES:
        assert (
            f"table_name='{table}' and column_name='demo_id'" in lowered
        )
        assert f"alter table `{table}` add column `demo_id`" in lowered


def test_phase18_keeps_all_workspace_scoped_indexes_guarded():
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    for table, index_name in DEMO_ID_INDEXES:
        assert (
            f"table_name='{table}' and index_name='{index_name}'" in sql
        )
        assert f"add index `{index_name}`" in sql
