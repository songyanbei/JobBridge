-- Phase 18: explicit demo_id scope on business and audit resources.
-- Additive migration. Existing production rows remain NULL and therefore keep
-- the legacy path. Apply after phase17_001_demo_control_plane.sql.

ALTER TABLE `user` ADD COLUMN IF NOT EXISTS `demo_id` VARCHAR(64) NULL COMMENT 'demo workspace id';
ALTER TABLE `job` ADD COLUMN IF NOT EXISTS `demo_id` VARCHAR(64) NULL COMMENT 'demo workspace id';
ALTER TABLE `resume` ADD COLUMN IF NOT EXISTS `demo_id` VARCHAR(64) NULL COMMENT 'demo workspace id';
ALTER TABLE `conversation_log` ADD COLUMN IF NOT EXISTS `demo_id` VARCHAR(64) NULL COMMENT 'demo workspace id';
ALTER TABLE `audit_log` ADD COLUMN IF NOT EXISTS `demo_id` VARCHAR(64) NULL COMMENT 'demo workspace id';
ALTER TABLE `event_log` ADD COLUMN IF NOT EXISTS `demo_id` VARCHAR(64) NULL COMMENT 'demo workspace id';
ALTER TABLE `wecom_inbound_event` ADD COLUMN IF NOT EXISTS `demo_id` VARCHAR(64) NULL COMMENT 'demo workspace id';
ALTER TABLE `wecom_outbound_outbox` ADD COLUMN IF NOT EXISTS `demo_id` VARCHAR(64) NULL COMMENT 'demo workspace id';
ALTER TABLE `action_execution` ADD COLUMN IF NOT EXISTS `demo_id` VARCHAR(64) NULL COMMENT 'demo workspace id';
ALTER TABLE `action_parse_artifact` ADD COLUMN IF NOT EXISTS `demo_id` VARCHAR(64) NULL COMMENT 'demo workspace id';
ALTER TABLE `contact_request` ADD COLUMN IF NOT EXISTS `demo_id` VARCHAR(64) NULL COMMENT 'demo workspace id';
ALTER TABLE `contact_grant` ADD COLUMN IF NOT EXISTS `demo_id` VARCHAR(64) NULL COMMENT 'demo workspace id';
ALTER TABLE `contact_delivery` ADD COLUMN IF NOT EXISTS `demo_id` VARCHAR(64) NULL COMMENT 'demo workspace id';
ALTER TABLE `contact_access_audit` ADD COLUMN IF NOT EXISTS `demo_id` VARCHAR(64) NULL COMMENT 'demo workspace id';
ALTER TABLE `recommendation_request` ADD COLUMN IF NOT EXISTS `demo_id` VARCHAR(64) NULL COMMENT 'demo workspace id';
ALTER TABLE `recommendation_search_attempt` ADD COLUMN IF NOT EXISTS `demo_id` VARCHAR(64) NULL COMMENT 'demo workspace id';
ALTER TABLE `recommendation_delivery` ADD COLUMN IF NOT EXISTS `demo_id` VARCHAR(64) NULL COMMENT 'demo workspace id';
ALTER TABLE `recommendation_impression` ADD COLUMN IF NOT EXISTS `demo_id` VARCHAR(64) NULL COMMENT 'demo workspace id';
ALTER TABLE `recommendation_exposure_daily` ADD COLUMN IF NOT EXISTS `demo_id` VARCHAR(64) NULL COMMENT 'demo workspace id';
ALTER TABLE `job_replacement` ADD COLUMN IF NOT EXISTS `demo_id` VARCHAR(64) NULL COMMENT 'demo workspace id';
ALTER TABLE `resume_replacement` ADD COLUMN IF NOT EXISTS `demo_id` VARCHAR(64) NULL COMMENT 'demo workspace id';
ALTER TABLE `resume_replacement_rollout_assignment` ADD COLUMN IF NOT EXISTS `demo_id` VARCHAR(64) NULL COMMENT 'demo workspace id';
ALTER TABLE `media_asset_lifecycle` ADD COLUMN IF NOT EXISTS `demo_id` VARCHAR(64) NULL COMMENT 'demo workspace id';
ALTER TABLE `target_cleanup_task` ADD COLUMN IF NOT EXISTS `demo_id` VARCHAR(64) NULL COMMENT 'demo workspace id';
ALTER TABLE `domain_outbox_event` ADD COLUMN IF NOT EXISTS `demo_id` VARCHAR(64) NULL COMMENT 'demo workspace id';

-- Recover the demo_id already present in synthetic User.extra from phase17.
UPDATE `user`
SET `demo_id` = JSON_UNQUOTE(JSON_EXTRACT(`extra`, '$.demo_id'))
WHERE `demo_id` IS NULL
  AND JSON_EXTRACT(`extra`, '$.demo_synthetic') = TRUE
  AND JSON_UNQUOTE(JSON_EXTRACT(`extra`, '$.demo_id')) IS NOT NULL;

-- The registry is authoritative for resources created before this migration.
UPDATE `job` j
JOIN `demo_resource` r
  ON r.`resource_type` = 'job' AND r.`target_id` = CAST(j.`id` AS CHAR)
SET j.`demo_id` = r.`demo_id`
WHERE j.`demo_id` IS NULL;

UPDATE `resume` r0
JOIN `demo_resource` r
  ON r.`resource_type` = 'resume' AND r.`target_id` = CAST(r0.`id` AS CHAR)
SET r0.`demo_id` = r.`demo_id`
WHERE r0.`demo_id` IS NULL;

-- Indexes are intentionally scoped by workspace before actor/time fields so
-- all admin cleanup and demo reporting queries can stay exact and bounded.

-- MySQL does not support ``ADD INDEX IF NOT EXISTS``.  Keep every index
-- guarded through information_schema so a partially applied deployment can be
-- safely retried without turning a duplicate-key error into a failed release.
SET @ddl = IF(EXISTS(SELECT 1 FROM information_schema.statistics WHERE table_schema=@schema_name AND table_name='user' AND index_name='idx_user_demo'), 'SELECT 1', 'ALTER TABLE `user` ADD INDEX `idx_user_demo` (`demo_id`,`external_userid`)'); PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @ddl = IF(EXISTS(SELECT 1 FROM information_schema.statistics WHERE table_schema=@schema_name AND table_name='job' AND index_name='idx_job_demo_owner'), 'SELECT 1', 'ALTER TABLE `job` ADD INDEX `idx_job_demo_owner` (`demo_id`,`owner_userid`,`id`)'); PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @ddl = IF(EXISTS(SELECT 1 FROM information_schema.statistics WHERE table_schema=@schema_name AND table_name='resume' AND index_name='idx_resume_demo_owner'), 'SELECT 1', 'ALTER TABLE `resume` ADD INDEX `idx_resume_demo_owner` (`demo_id`,`owner_userid`,`id`)'); PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @ddl = IF(EXISTS(SELECT 1 FROM information_schema.statistics WHERE table_schema=@schema_name AND table_name='conversation_log' AND index_name='idx_conversation_demo_time'), 'SELECT 1', 'ALTER TABLE `conversation_log` ADD INDEX `idx_conversation_demo_time` (`demo_id`,`created_at`,`id`)'); PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @ddl = IF(EXISTS(SELECT 1 FROM information_schema.statistics WHERE table_schema=@schema_name AND table_name='audit_log' AND index_name='idx_audit_demo_time'), 'SELECT 1', 'ALTER TABLE `audit_log` ADD INDEX `idx_audit_demo_time` (`demo_id`,`created_at`,`id`)'); PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @ddl = IF(EXISTS(SELECT 1 FROM information_schema.statistics WHERE table_schema=@schema_name AND table_name='event_log' AND index_name='idx_event_demo_time'), 'SELECT 1', 'ALTER TABLE `event_log` ADD INDEX `idx_event_demo_time` (`demo_id`,`occurred_at`,`id`)'); PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @ddl = IF(EXISTS(SELECT 1 FROM information_schema.statistics WHERE table_schema=@schema_name AND table_name='wecom_inbound_event' AND index_name='idx_inbound_demo_status'), 'SELECT 1', 'ALTER TABLE `wecom_inbound_event` ADD INDEX `idx_inbound_demo_status` (`demo_id`,`status`,`id`)'); PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @ddl = IF(EXISTS(SELECT 1 FROM information_schema.statistics WHERE table_schema=@schema_name AND table_name='wecom_outbound_outbox' AND index_name='idx_outbox_demo_status'), 'SELECT 1', 'ALTER TABLE `wecom_outbound_outbox` ADD INDEX `idx_outbox_demo_status` (`demo_id`,`status`,`id`)'); PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @ddl = IF(EXISTS(SELECT 1 FROM information_schema.statistics WHERE table_schema=@schema_name AND table_name='action_execution' AND index_name='idx_action_execution_demo'), 'SELECT 1', 'ALTER TABLE `action_execution` ADD INDEX `idx_action_execution_demo` (`demo_id`,`created_at`,`id`)'); PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @ddl = IF(EXISTS(SELECT 1 FROM information_schema.statistics WHERE table_schema=@schema_name AND table_name='action_parse_artifact' AND index_name='idx_action_parse_demo'), 'SELECT 1', 'ALTER TABLE `action_parse_artifact` ADD INDEX `idx_action_parse_demo` (`demo_id`,`created_at`,`parse_ref`)'); PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @ddl = IF(EXISTS(SELECT 1 FROM information_schema.statistics WHERE table_schema=@schema_name AND table_name='contact_request' AND index_name='idx_contact_request_demo'), 'SELECT 1', 'ALTER TABLE `contact_request` ADD INDEX `idx_contact_request_demo` (`demo_id`,`created_at`,`request_id`)'); PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @ddl = IF(EXISTS(SELECT 1 FROM information_schema.statistics WHERE table_schema=@schema_name AND table_name='contact_grant' AND index_name='idx_contact_grant_demo'), 'SELECT 1', 'ALTER TABLE `contact_grant` ADD INDEX `idx_contact_grant_demo` (`demo_id`,`created_at`,`grant_id`)'); PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @ddl = IF(EXISTS(SELECT 1 FROM information_schema.statistics WHERE table_schema=@schema_name AND table_name='contact_delivery' AND index_name='idx_contact_delivery_demo'), 'SELECT 1', 'ALTER TABLE `contact_delivery` ADD INDEX `idx_contact_delivery_demo` (`demo_id`,`created_at`,`delivery_id`)'); PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @ddl = IF(EXISTS(SELECT 1 FROM information_schema.statistics WHERE table_schema=@schema_name AND table_name='contact_access_audit' AND index_name='idx_contact_audit_demo'), 'SELECT 1', 'ALTER TABLE `contact_access_audit` ADD INDEX `idx_contact_audit_demo` (`demo_id`,`created_at`,`id`)'); PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @ddl = IF(EXISTS(SELECT 1 FROM information_schema.statistics WHERE table_schema=@schema_name AND table_name='recommendation_request' AND index_name='idx_recommendation_request_demo_time'), 'SELECT 1', 'ALTER TABLE `recommendation_request` ADD INDEX `idx_recommendation_request_demo_time` (`demo_id`,`created_at`,`request_id`)'); PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @ddl = IF(EXISTS(SELECT 1 FROM information_schema.statistics WHERE table_schema=@schema_name AND table_name='recommendation_search_attempt' AND index_name='idx_recommendation_attempt_demo_time'), 'SELECT 1', 'ALTER TABLE `recommendation_search_attempt` ADD INDEX `idx_recommendation_attempt_demo_time` (`demo_id`,`created_at`,`attempt_id`)'); PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @ddl = IF(EXISTS(SELECT 1 FROM information_schema.statistics WHERE table_schema=@schema_name AND table_name='recommendation_delivery' AND index_name='idx_recommendation_delivery_demo_status'), 'SELECT 1', 'ALTER TABLE `recommendation_delivery` ADD INDEX `idx_recommendation_delivery_demo_status` (`demo_id`,`status`,`delivery_id`)'); PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @ddl = IF(EXISTS(SELECT 1 FROM information_schema.statistics WHERE table_schema=@schema_name AND table_name='recommendation_impression' AND index_name='idx_recommendation_impression_demo_time'), 'SELECT 1', 'ALTER TABLE `recommendation_impression` ADD INDEX `idx_recommendation_impression_demo_time` (`demo_id`,`exposed_at`,`id`)'); PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @ddl = IF(EXISTS(SELECT 1 FROM information_schema.statistics WHERE table_schema=@schema_name AND table_name='recommendation_exposure_daily' AND index_name='idx_recommendation_exposure_demo'), 'SELECT 1', 'ALTER TABLE `recommendation_exposure_daily` ADD INDEX `idx_recommendation_exposure_demo` (`demo_id`,`stat_date`,`target_type`,`target_id`)'); PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @ddl = IF(EXISTS(SELECT 1 FROM information_schema.statistics WHERE table_schema=@schema_name AND table_name='job_replacement' AND index_name='idx_replacement_demo_owner'), 'SELECT 1', 'ALTER TABLE `job_replacement` ADD INDEX `idx_replacement_demo_owner` (`demo_id`,`owner_userid`,`created_at`)'); PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @ddl = IF(EXISTS(SELECT 1 FROM information_schema.statistics WHERE table_schema=@schema_name AND table_name='resume_replacement' AND index_name='idx_resume_replacement_demo_owner'), 'SELECT 1', 'ALTER TABLE `resume_replacement` ADD INDEX `idx_resume_replacement_demo_owner` (`demo_id`,`owner_userid`,`created_at`)'); PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @ddl = IF(EXISTS(SELECT 1 FROM information_schema.statistics WHERE table_schema=@schema_name AND table_name='resume_replacement_rollout_assignment' AND index_name='idx_resume_rollout_demo_owner'), 'SELECT 1', 'ALTER TABLE `resume_replacement_rollout_assignment` ADD INDEX `idx_resume_rollout_demo_owner` (`demo_id`,`owner_userid`,`created_at`)'); PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @ddl = IF(EXISTS(SELECT 1 FROM information_schema.statistics WHERE table_schema=@schema_name AND table_name='media_asset_lifecycle' AND index_name='idx_media_demo_owner'), 'SELECT 1', 'ALTER TABLE `media_asset_lifecycle` ADD INDEX `idx_media_demo_owner` (`demo_id`,`owner_userid`,`created_at`)'); PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @ddl = IF(EXISTS(SELECT 1 FROM information_schema.statistics WHERE table_schema=@schema_name AND table_name='target_cleanup_task' AND index_name='idx_target_cleanup_demo'), 'SELECT 1', 'ALTER TABLE `target_cleanup_task` ADD INDEX `idx_target_cleanup_demo` (`demo_id`,`status`,`id`)'); PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @ddl = IF(EXISTS(SELECT 1 FROM information_schema.statistics WHERE table_schema=@schema_name AND table_name='domain_outbox_event' AND index_name='idx_domain_outbox_demo'), 'SELECT 1', 'ALTER TABLE `domain_outbox_event` ADD INDEX `idx_domain_outbox_demo` (`demo_id`,`created_at`,`id`)'); PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
