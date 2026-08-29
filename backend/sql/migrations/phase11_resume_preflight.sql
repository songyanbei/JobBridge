-- Phase 11 resume lifecycle preflight for the main@64bbc079 baseline.
--
-- READ-ONLY CONTRACT
--   * Every executable statement is SELECT or WITH ... SELECT.
--   * Results contain aggregate counts, booleans, and server metadata only.
--   * Results never expose userid, resume text, phone numbers, media URLs,
--     object keys, or raw target IDs.
--   * Run with a database account that has SELECT only. Build/worker probes
--     are deliberately outside SQL and must be archived by the Phase 11 runner.
--
-- MySQL 8.0+ is required for JSON_TABLE and CTE support. This script targets
-- the current pre-Phase-11 schema and does not reference Phase 11 columns/tables.

SELECT
    UTC_TIMESTAMP(6) AS snapshot_at_utc,
    VERSION() AS mysql_version,
    @@version_comment AS mysql_vendor_comment,
    @@session.time_zone AS session_time_zone,
    @@global.time_zone AS global_time_zone;

SELECT
    COUNT(DISTINCT table_name) AS required_table_count_present,
    13 - COUNT(DISTINCT table_name) AS required_table_count_missing
FROM information_schema.tables
WHERE table_schema = DATABASE()
  AND table_name IN (
      'resume',
      'job',
      'system_config',
      'media_asset_lifecycle',
      'target_cleanup_task',
      'recommendation_request',
      'recommendation_search_attempt',
      'recommendation_delivery',
      'recommendation_impression',
      'recommendation_exposure_daily',
      'wecom_outbound_outbox',
      'conversation_log',
      'event_log'
  );

WITH snapshot AS (
    SELECT UTC_TIMESTAMP(6) AS now_utc
)
SELECT
    COUNT(*) AS resume_total_count,
    COALESCE(SUM(r.audit_status = 'passed' AND r.deleted_at IS NULL AND r.expires_at > s.now_utc), 0)
        AS online_passed_count,
    COALESCE(SUM(r.audit_status = 'pending' AND r.deleted_at IS NULL), 0)
        AS pending_not_deleted_count,
    COALESCE(SUM(r.audit_status = 'rejected' AND r.deleted_at IS NULL), 0)
        AS rejected_not_deleted_count,
    COALESCE(SUM(r.deleted_at IS NOT NULL), 0) AS soft_deleted_count,
    COALESCE(SUM(r.expires_at IS NULL), 0) AS expires_at_null_count,
    COALESCE(SUM(r.expires_at <= s.now_utc AND r.deleted_at IS NULL), 0)
        AS expired_not_soft_deleted_count
FROM resume AS r
CROSS JOIN snapshot AS s;

WITH snapshot AS (
    SELECT UTC_TIMESTAMP(6) AS now_utc
), active_by_owner AS (
    SELECT r.owner_userid, COUNT(*) AS active_count
    FROM resume AS r
    CROSS JOIN snapshot AS s
    WHERE r.audit_status = 'passed'
      AND r.deleted_at IS NULL
      AND r.expires_at > s.now_utc
    GROUP BY r.owner_userid
)
SELECT
    COUNT(*) AS owner_with_multiple_online_resumes_count,
    COALESCE(SUM(active_count), 0) AS resumes_in_multiple_online_owner_groups
FROM active_by_owner
WHERE active_count > 1;

SELECT
    COALESCE(SUM(config_key = 'ttl.resume.days'), 0) AS resume_ttl_key_count,
    COALESCE(SUM(
        config_key = 'ttl.resume.days'
        AND value_type = 'int'
        AND config_value REGEXP '^[0-9]+$'
        AND CAST(config_value AS UNSIGNED) BETWEEN 1 AND 3650
    ), 0) AS resume_ttl_valid_count,
    COALESCE(SUM(config_key = 'ttl.hard_delete.delay_days'), 0)
        AS hard_delete_delay_key_count,
    COALESCE(SUM(
        config_key = 'ttl.hard_delete.delay_days'
        AND value_type = 'int'
        AND config_value REGEXP '^[0-9]+$'
        AND CAST(config_value AS UNSIGNED) BETWEEN 0 AND 3650
    ), 0) AS hard_delete_delay_valid_count
FROM system_config
WHERE config_key IN ('ttl.resume.days', 'ttl.hard_delete.delay_days');

-- SQL intentionally checks JSON shape only. Canonical media reconciliation is
-- performed by backend/scripts/phase11_resume_preflight_media.py, which reuses
-- the application's trusted-origin and local-prefix normalization contract.
SELECT
    COUNT(*) AS resume_with_non_null_images_count,
    COALESCE(SUM(JSON_TYPE(images) <> 'ARRAY'), 0) AS resume_images_not_array_count,
    COALESCE(SUM(JSON_TYPE(images) = 'ARRAY' AND JSON_LENGTH(images) = 0), 0)
        AS resume_images_empty_array_count
FROM resume
WHERE images IS NOT NULL;

WITH image_shapes AS (
    SELECT
        r.id AS resume_id,
        refs.ordinality,
        JSON_TYPE(
            JSON_EXTRACT(
                COALESCE(r.images, JSON_ARRAY()),
                CONCAT('$[', refs.ordinality - 1, ']')
            )
        ) AS value_type,
        refs.raw_reference
    FROM resume AS r
    JOIN JSON_TABLE(
        COALESCE(r.images, JSON_ARRAY()),
        '$[*]' COLUMNS (
            ordinality FOR ORDINALITY,
            raw_reference VARCHAR(2048) PATH '$' NULL ON EMPTY NULL ON ERROR
        )
    ) AS refs
    WHERE JSON_TYPE(r.images) = 'ARRAY'
)
SELECT
    COUNT(*) AS resume_image_json_item_count,
    COUNT(DISTINCT resume_id) AS resume_with_images_count,
    COALESCE(SUM(value_type <> 'STRING' OR raw_reference IS NULL OR raw_reference = ''), 0)
        AS resume_image_non_string_or_empty_item_count
FROM image_shapes;

WITH recommendation_resume_refs AS (
    SELECT 'request_served' AS source_name, ids.target_id
    FROM recommendation_request AS req
    JOIN JSON_TABLE(
        COALESCE(req.served_top_ids, JSON_ARRAY()),
        '$[*]' COLUMNS (
            target_id BIGINT UNSIGNED PATH '$' NULL ON EMPTY NULL ON ERROR
        )
    ) AS ids
    WHERE req.direction = 'search_worker'

    UNION ALL

    SELECT 'request_shadow' AS source_name, ids.target_id
    FROM recommendation_request AS req
    JOIN JSON_TABLE(
        COALESCE(req.shadow_top_ids, JSON_ARRAY()),
        '$[*]' COLUMNS (
            target_id BIGINT UNSIGNED PATH '$' NULL ON EMPTY NULL ON ERROR
        )
    ) AS ids
    WHERE req.direction = 'search_worker'

    UNION ALL

    SELECT 'attempt_candidate' AS source_name, ids.target_id
    FROM recommendation_search_attempt AS attempt
    JOIN recommendation_request AS req ON req.request_id = attempt.request_id
    JOIN JSON_TABLE(
        COALESCE(attempt.candidate_ids, JSON_ARRAY()),
        '$[*]' COLUMNS (
            target_id BIGINT UNSIGNED PATH '$' NULL ON EMPTY NULL ON ERROR
        )
    ) AS ids
    WHERE req.direction = 'search_worker'

    UNION ALL

    SELECT 'attempt_precision' AS source_name, ids.target_id
    FROM recommendation_search_attempt AS attempt
    JOIN recommendation_request AS req ON req.request_id = attempt.request_id
    JOIN JSON_TABLE(
        COALESCE(attempt.precision_pool_ids, JSON_ARRAY()),
        '$[*]' COLUMNS (
            target_id BIGINT UNSIGNED PATH '$' NULL ON EMPTY NULL ON ERROR
        )
    ) AS ids
    WHERE req.direction = 'search_worker'

    UNION ALL

    SELECT 'delivery_context' AS source_name, items.target_id
    FROM recommendation_delivery AS delivery
    JOIN JSON_TABLE(
        COALESCE(delivery.recommendation_context, JSON_OBJECT()),
        '$.items[*]' COLUMNS (
            target_type VARCHAR(16) PATH '$.target_type' NULL ON EMPTY NULL ON ERROR,
            target_id BIGINT UNSIGNED PATH '$.target_id' NULL ON EMPTY NULL ON ERROR
        )
    ) AS items
    WHERE items.target_type = 'resume'

    UNION ALL

    SELECT 'outbox_delivery_context' AS source_name, items.target_id
    FROM wecom_outbound_outbox AS outbox
    JOIN recommendation_delivery AS delivery
      ON delivery.delivery_id = outbox.recommendation_delivery_id
    JOIN JSON_TABLE(
        COALESCE(delivery.recommendation_context, JSON_OBJECT()),
        '$.items[*]' COLUMNS (
            target_type VARCHAR(16) PATH '$.target_type' NULL ON EMPTY NULL ON ERROR,
            target_id BIGINT UNSIGNED PATH '$.target_id' NULL ON EMPTY NULL ON ERROR
        )
    ) AS items
    WHERE items.target_type = 'resume'

    UNION ALL

    SELECT 'conversation_delivery_context' AS source_name, items.target_id
    FROM conversation_log AS log_row
    JOIN recommendation_delivery AS delivery
      ON delivery.delivery_id = log_row.recommendation_delivery_id
    JOIN JSON_TABLE(
        COALESCE(delivery.recommendation_context, JSON_OBJECT()),
        '$.items[*]' COLUMNS (
            target_type VARCHAR(16) PATH '$.target_type' NULL ON EMPTY NULL ON ERROR,
            target_id BIGINT UNSIGNED PATH '$.target_id' NULL ON EMPTY NULL ON ERROR
        )
    ) AS items
    WHERE items.target_type = 'resume'

    UNION ALL

    SELECT 'impression' AS source_name, impression.target_id
    FROM recommendation_impression AS impression
    WHERE impression.target_type = 'resume'

    UNION ALL

    SELECT 'exposure_daily' AS source_name, exposure.target_id
    FROM recommendation_exposure_daily AS exposure
    WHERE exposure.target_type = 'resume'

    UNION ALL

    SELECT 'event_log' AS source_name, event_row.target_id
    FROM event_log AS event_row
    WHERE event_row.target_type = 'resume'
), valid_refs AS (
    SELECT source_name, target_id
    FROM recommendation_resume_refs
    WHERE target_id IS NOT NULL
), orphan_refs AS (
    SELECT DISTINCT refs.target_id
    FROM valid_refs AS refs
    LEFT JOIN resume AS r ON r.id = refs.target_id
    WHERE r.id IS NULL
)
SELECT
    (SELECT COUNT(*) FROM valid_refs) AS recommendation_resume_reference_row_count,
    (SELECT COUNT(DISTINCT CONCAT(source_name, ':', target_id)) FROM valid_refs)
        AS recommendation_resume_reference_source_target_count,
    (SELECT COUNT(DISTINCT target_id) FROM valid_refs)
        AS recommendation_resume_distinct_target_count,
    (SELECT COUNT(*) FROM orphan_refs) AS orphan_resume_target_count;

SELECT
    COUNT(*) AS resume_target_cleanup_task_count,
    COALESCE(SUM(status = 'pending'), 0) AS resume_target_cleanup_pending_count,
    COALESCE(SUM(status = 'processing'), 0) AS resume_target_cleanup_processing_count,
    COALESCE(SUM(status = 'retry_wait'), 0) AS resume_target_cleanup_retry_wait_count,
    COALESCE(SUM(status = 'dead_letter'), 0) AS resume_target_cleanup_dead_letter_count,
    COALESCE(SUM(status = 'succeeded'), 0) AS resume_target_cleanup_succeeded_count
FROM target_cleanup_task
WHERE target_type = 'resume';

SELECT
    COUNT(*) AS resume_media_lifecycle_count,
    COALESCE(SUM(state = 'pending'), 0) AS resume_media_pending_count,
    COALESCE(SUM(state = 'attached'), 0) AS resume_media_attached_count,
    COALESCE(SUM(state = 'delete_pending'), 0) AS resume_media_delete_pending_count,
    COALESCE(SUM(state = 'dead_letter'), 0) AS resume_media_dead_letter_count,
    COALESCE(SUM(state = 'deleted'), 0) AS resume_media_deleted_count
FROM media_asset_lifecycle
WHERE entity_type = 'resume';

SELECT
    COUNT(*) AS soft_deleted_resume_without_cleanup_task_count
FROM resume AS r
LEFT JOIN target_cleanup_task AS task
  ON task.target_type = 'resume'
 AND task.target_id = r.id
WHERE r.deleted_at IS NOT NULL
  AND task.id IS NULL;

-- Build compatibility is intentionally not inferred from database data.
-- The Phase 11 runner must separately archive one probe per live API/writer:
-- build_number, build_sha, and capabilities including
-- resume_nullable_dto and resume_lifecycle_double_write.
SELECT
    1 AS external_build_probe_required,
    0 AS external_build_probe_collected_by_this_sql;
