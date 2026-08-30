-- A0: durable Action result and parse references. Additive and repeatable on MySQL 8.
SET @schema_name = DATABASE();

SET @ddl = IF(
  EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='action_execution' AND column_name='action_version'),
  'SELECT 1',
  'ALTER TABLE `action_execution`
     ADD COLUMN `actor_userid` VARCHAR(64) NULL AFTER `action_name`,
     ADD COLUMN `action_version` VARCHAR(32) NOT NULL DEFAULT ''v1'' AFTER `result_digest`,
     ADD COLUMN `result_ref_type` VARCHAR(32) NULL AFTER `action_version`,
     ADD COLUMN `request_id` CHAR(36) NULL AFTER `result_ref_type`,
     ADD COLUMN `snapshot_id` CHAR(36) NULL AFTER `request_id`,
     ADD COLUMN `delivery_ids` JSON NULL AFTER `snapshot_id`,
     ADD COLUMN `outbox_ids` JSON NULL AFTER `delivery_ids`,
     ADD COLUMN `session_commit_id` CHAR(36) NULL AFTER `outbox_ids`,
     ADD COLUMN `result_schema_version` VARCHAR(32) NULL AFTER `session_commit_id`,
     ADD COLUMN `failure_code` VARCHAR(64) NULL AFTER `result_schema_version`,
     ADD COLUMN `replay_count` INT UNSIGNED NOT NULL DEFAULT 0 AFTER `failure_code`,
     ADD COLUMN `last_replayed_at` DATETIME(6) NULL AFTER `replay_count`,
     ADD COLUMN `parse_ref` CHAR(36) NULL AFTER `last_replayed_at`,
     ADD COLUMN `parse_digest` CHAR(64) NULL AFTER `parse_ref`,
     ADD COLUMN `parse_version` VARCHAR(32) NULL AFTER `parse_digest`,
     ADD COLUMN `parse_expires_at` DATETIME(6) NULL AFTER `parse_version`'
);
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @ddl = IF(
  EXISTS (SELECT 1 FROM information_schema.statistics WHERE table_schema=@schema_name AND table_name='action_execution' AND index_name='idx_action_execution_request_snapshot'),
  'SELECT 1',
  'ALTER TABLE `action_execution` ADD KEY `idx_action_execution_request_snapshot` (`request_id`,`snapshot_id`)'
);
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

CREATE TABLE IF NOT EXISTS `action_parse_artifact` (
  `parse_ref` CHAR(36) NOT NULL,
  `turn_id` CHAR(36) NOT NULL,
  `actor_userid` VARCHAR(64) NOT NULL,
  `parse_digest` CHAR(64) NOT NULL,
  `schema_version` VARCHAR(32) NOT NULL,
  `classifier_version` VARCHAR(64) NOT NULL,
  `session_version` BIGINT UNSIGNED NULL,
  `payload` JSON NOT NULL,
  `expires_at` DATETIME(6) NOT NULL,
  `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`parse_ref`),
  UNIQUE KEY `uk_action_parse_turn_digest` (`turn_id`,`parse_digest`),
  KEY `idx_action_parse_expires` (`expires_at`,`parse_ref`),
  KEY `idx_action_parse_turn` (`turn_id`,`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='PII-free Action parse artifacts';
