-- Ensure the recommendation visibility policy exists without overwriting
-- an operator-managed value. Run before serving traffic.
INSERT INTO `system_config`
    (`config_key`, `config_value`, `value_type`, `description`)
VALUES
    ('visibility.recommendation_fields',
     '{"schema_version":1,"revision":1,"job_search":{"worker":["hiring_company","job_category","salary"],"factory":[],"broker":["hiring_company","job_category","salary","city","district","address","benefits","shift","contact_person","phone","publisher_company"]},"candidate_search":{"worker":[],"factory":["display_name","gender_age","expected_job_categories","salary_expectation","expected_cities","phone"],"broker":["display_name","gender_age","expected_job_categories","salary_expectation","expected_cities","phone"]}}',
     'json', '推荐岗位/求职者推荐可见字段策略')
ON DUPLICATE KEY UPDATE config_key = VALUES(config_key);
