-- Ensure the recommendation visibility policy exists without overwriting
-- an operator-managed value. Run before serving traffic.
INSERT INTO `system_config`
    (`config_key`, `config_value`, `value_type`, `description`)
VALUES
    ('visibility.recommendation_fields',
     '{"schema_version":1,"revision":1,"job_search":{"worker":["hiring_company","job_category","salary"],"factory":[],"broker":["hiring_company","job_category","salary","city","district","address","benefits","shift","contact_person","phone","publisher_company"]},"candidate_search":{"worker":[],"factory":["display_name","gender_age","expected_job_categories","salary_expectation","expected_cities","phone"],"broker":["display_name","gender_age","expected_job_categories","salary_expectation","expected_cities","phone"]}}',
     'json', '推荐岗位/求职者推荐可见字段策略')
ON DUPLICATE KEY UPDATE config_key = VALUES(config_key);

-- Record the immutable bootstrap revision so readiness can prove that the
-- active policy has a complete successful audit. Re-running is a no-op.
INSERT INTO `audit_log`
    (`target_type`, `target_id`, `action`, `reason`, `operator`, `snapshot`)
SELECT
    'system', sc.config_key, 'manual_edit', 'visibility_policy_bootstrap',
    'migration',
    JSON_OBJECT(
        'before', NULL,
        'after', JSON_OBJECT(
            'config_value', CAST(sc.config_value AS JSON),
            'schema_version', CAST(JSON_UNQUOTE(JSON_EXTRACT(sc.config_value, '$.schema_version')) AS UNSIGNED),
            'revision', CAST(JSON_UNQUOTE(JSON_EXTRACT(sc.config_value, '$.revision')) AS UNSIGNED)
        )
    )
FROM `system_config` sc
WHERE sc.config_key = 'visibility.recommendation_fields'
  AND JSON_VALID(sc.config_value)
  AND CAST(JSON_UNQUOTE(JSON_EXTRACT(sc.config_value, '$.revision')) AS UNSIGNED) = 1
  AND NOT EXISTS (
      SELECT 1 FROM `audit_log` al
      WHERE al.target_type = 'system'
        AND al.target_id = sc.config_key
        AND al.action = 'manual_edit'
        AND CAST(JSON_UNQUOTE(JSON_EXTRACT(al.snapshot, '$.after.revision')) AS UNSIGNED) = 1
  );
