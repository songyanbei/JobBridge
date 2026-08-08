-- v0.5 additive migration. Deploy nullable readers before executing this file.
CREATE TABLE `phase10_job_lifecycle_backup` AS
SELECT `id` AS `job_id`, `audit_status`, `expires_at`, `deleted_at`,
       `delist_reason`, `version`
FROM `job`;
ALTER TABLE `phase10_job_lifecycle_backup`
  ADD PRIMARY KEY (`job_id`);

ALTER TABLE `job` MODIFY COLUMN `expires_at` DATETIME NULL COMMENT '激活后的业务过期时间';
ALTER TABLE `job` ADD COLUMN `activated_at` DATETIME NULL AFTER `updated_at`;
ALTER TABLE `job` ADD COLUMN `candidate_expires_at` DATETIME NULL AFTER `activated_at`;
ALTER TABLE `job` MODIFY COLUMN `delist_reason` ENUM('filled','manual_delist','expired','replaced') NULL;
CREATE INDEX `idx_job_candidate_expiry` ON `job` (`audit_status`, `candidate_expires_at`);
CREATE TABLE `job_replacement` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT, `operation_id` CHAR(36) NOT NULL,
  `source_msg_id` VARCHAR(128) NOT NULL, `owner_userid` VARCHAR(64) NOT NULL,
  `old_job_id` BIGINT UNSIGNED NOT NULL, `new_job_id` BIGINT UNSIGNED NOT NULL,
  `old_job_version` INT UNSIGNED NOT NULL, `old_expires_at` DATETIME NULL,
  `old_business_digest` CHAR(64) NOT NULL, `old_business_digest_version` TINYINT UNSIGNED NOT NULL DEFAULT 1,
  `review_outcome` ENUM('pending','passed','rejected') NOT NULL,
  `reviewed_at` DATETIME NULL, `reviewed_by` VARCHAR(64) NULL,
  `lifecycle_status` ENUM('awaiting_review','activated','closed','conflict') NOT NULL,
  `active_old_job_id` BIGINT UNSIGNED NULL, `closed_reason` VARCHAR(64) NULL,
  `conflict_reason` VARCHAR(255) NULL, `activated_at` DATETIME NULL, `candidate_cleaned_at` DATETIME NULL,
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`), UNIQUE KEY `uq_replacement_operation` (`operation_id`), UNIQUE KEY `uq_replacement_message` (`source_msg_id`),
  UNIQUE KEY `uq_replacement_new_job` (`new_job_id`), UNIQUE KEY `uq_replacement_active_old_job` (`active_old_job_id`),
  KEY `idx_replacement_old_status` (`old_job_id`,`lifecycle_status`),
  KEY `idx_replacement_owner_created` (`owner_userid`,`created_at`),
  KEY `idx_replacement_lifecycle_created` (`lifecycle_status`,`created_at`),
  KEY `idx_replacement_review_created` (`review_outcome`,`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE `target_cleanup_task` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `operation_id` CHAR(36) NOT NULL,
  `target_type` VARCHAR(32) NOT NULL,
  `target_id` BIGINT UNSIGNED NOT NULL,
  `reason` VARCHAR(32) NOT NULL,
  `reason_history` JSON NULL,
  `status` ENUM('pending','processing','retry_wait','succeeded','dead_letter') NOT NULL DEFAULT 'pending',
  `delivery_ids` JSON NULL,
  `db_redacted_at` DATETIME NULL,
  `conversation_redacted_at` DATETIME NULL,
  `session_invalidated_at` DATETIME NULL,
  `attempt_count` INT UNSIGNED NOT NULL DEFAULT 0,
  `next_attempt_at` DATETIME NULL,
  `last_error` VARCHAR(255) NULL,
  `lease_owner` VARCHAR(64) NULL,
  `lease_expires_at` DATETIME NULL,
  `completed_at` DATETIME NULL,
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_cleanup_operation` (`operation_id`),
  UNIQUE KEY `uq_cleanup_target` (`target_type`,`target_id`),
  KEY `idx_target_cleanup_ready` (`status`,`next_attempt_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE `media_asset_lifecycle` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `object_key` VARCHAR(512) NOT NULL,
  `operation_id` CHAR(36) NULL,
  `owner_userid` VARCHAR(64) NOT NULL,
  `entity_type` ENUM('job','resume') NULL,
  `entity_id` BIGINT UNSIGNED NULL,
  `state` ENUM('pending','attached','delete_pending','deleted') NOT NULL DEFAULT 'pending',
  `draft_expires_at` DATETIME NULL,
  `attempt_count` INT UNSIGNED NOT NULL DEFAULT 0,
  `next_attempt_at` DATETIME NULL,
  `last_error` VARCHAR(255) NULL,
  `lease_owner` VARCHAR(64) NULL,
  `lease_expires_at` DATETIME NULL,
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted_at` DATETIME NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_media_object_key` (`object_key`),
  KEY `idx_media_operation` (`operation_id`),
  KEY `idx_media_entity` (`entity_type`,`entity_id`,`state`),
  KEY `idx_media_cleanup` (`state`,`next_attempt_at`),
  KEY `idx_media_draft_expiry` (`state`,`draft_expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

SET @phase10_migration_time = NOW();
SET @phase10_candidate_days = COALESCE((
  SELECT CASE
    WHEN `config_value` REGEXP '^[0-9]+$'
     AND CAST(`config_value` AS UNSIGNED) BETWEEN 1 AND 365
    THEN CAST(`config_value` AS UNSIGNED)
    ELSE 7 END
  FROM `system_config`
  WHERE `config_key` = 'ttl.job.candidate.days'
  LIMIT 1
), 7);

UPDATE `job`
SET `activated_at` = COALESCE(`audited_at`, `created_at`, `updated_at`),
    `version` = `version` + 1
WHERE `deleted_at` IS NOT NULL;

UPDATE `job`
SET `activated_at` = COALESCE(`audited_at`, `created_at`),
    `version` = `version` + 1
WHERE `deleted_at` IS NULL AND `audit_status` = 'passed';

UPDATE `job`
SET `activated_at` = NULL,
    `expires_at` = NULL,
    `candidate_expires_at` = DATE_ADD(@phase10_migration_time, INTERVAL @phase10_candidate_days DAY),
    `version` = `version` + 1
WHERE `deleted_at` IS NULL AND `audit_status` IN ('pending', 'rejected');

-- Deployment gate: this query must return zero rows before writes resume.
SELECT `id` FROM `job`
WHERE (`deleted_at` IS NULL AND `audit_status` = 'passed'
       AND (`activated_at` IS NULL OR `expires_at` IS NULL))
   OR (`deleted_at` IS NULL AND `audit_status` IN ('pending', 'rejected')
       AND (`activated_at` IS NOT NULL OR `expires_at` IS NOT NULL
            OR `candidate_expires_at` IS NULL));
