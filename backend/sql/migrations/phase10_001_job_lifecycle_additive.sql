-- v0.5 additive migration. Deploy nullable readers before executing this file.
CREATE TABLE `phase10_job_lifecycle_backup` AS
SELECT `id` AS `job_id`, `audit_status`, `expires_at`, `deleted_at`,
       `delist_reason`, `version`, `created_at` AS `source_created_at`,
       `updated_at` AS `source_updated_at`, `audited_at` AS `source_audited_at`
FROM `job`;
ALTER TABLE `phase10_job_lifecycle_backup`
  ADD PRIMARY KEY (`job_id`),
  MODIFY COLUMN `source_updated_at` DATETIME NOT NULL;

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
  `old_business_digest` CHAR(64) NOT NULL, `old_business_digest_version` TINYINT UNSIGNED NOT NULL DEFAULT 2,
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
  `state` ENUM('pending','attached','delete_pending','deleted','dead_letter') NOT NULL DEFAULT 'pending',
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

CREATE TABLE `phase10_migration_control` (
  `id` TINYINT UNSIGNED NOT NULL,
  `writes_blocked` TINYINT(1) NOT NULL DEFAULT 0,
  `backup_rows` BIGINT UNSIGNED NULL,
  `backup_checksum` BIGINT UNSIGNED NULL,
  `source_soft_deleted_rows` BIGINT UNSIGNED NULL,
  `source_passed_online_rows` BIGINT UNSIGNED NULL,
  `source_candidate_rows` BIGINT UNSIGNED NULL,
  `expected_live_checksum` BIGINT UNSIGNED NULL,
  PRIMARY KEY (`id`),
  CONSTRAINT `chk_phase10_writes_blocked` CHECK (`writes_blocked` IN (0, 1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
INSERT INTO `phase10_migration_control` (`id`, `writes_blocked`) VALUES (1, 0);

DELIMITER $$
CREATE PROCEDURE `phase10_assert_writes_allowed`()
SQL SECURITY INVOKER
BEGIN
  DECLARE blocked TINYINT DEFAULT 1;
  SELECT `writes_blocked` INTO blocked
  FROM `phase10_migration_control` WHERE `id` = 1 FOR SHARE;
  IF blocked = 1
     AND COALESCE(@phase10_destructive_down_authorized, 0) <> 1 THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'phase10_destructive_down_in_progress';
  END IF;
END$$
CREATE TRIGGER `phase10_job_insert_fence` BEFORE INSERT ON `job`
FOR EACH ROW CALL `phase10_assert_writes_allowed`()$$
CREATE TRIGGER `phase10_job_update_fence` BEFORE UPDATE ON `job`
FOR EACH ROW CALL `phase10_assert_writes_allowed`()$$
CREATE TRIGGER `phase10_job_delete_fence` BEFORE DELETE ON `job`
FOR EACH ROW CALL `phase10_assert_writes_allowed`()$$
CREATE TRIGGER `phase10_inbound_insert_fence` BEFORE INSERT ON `wecom_inbound_event`
FOR EACH ROW CALL `phase10_assert_writes_allowed`()$$
CREATE TRIGGER `phase10_inbound_update_fence` BEFORE UPDATE ON `wecom_inbound_event`
FOR EACH ROW CALL `phase10_assert_writes_allowed`()$$
CREATE TRIGGER `phase10_inbound_delete_fence` BEFORE DELETE ON `wecom_inbound_event`
FOR EACH ROW CALL `phase10_assert_writes_allowed`()$$
CREATE TRIGGER `phase10_replacement_insert_fence` BEFORE INSERT ON `job_replacement`
FOR EACH ROW CALL `phase10_assert_writes_allowed`()$$
CREATE TRIGGER `phase10_cleanup_insert_fence` BEFORE INSERT ON `target_cleanup_task`
FOR EACH ROW CALL `phase10_assert_writes_allowed`()$$
CREATE TRIGGER `phase10_media_insert_fence` BEFORE INSERT ON `media_asset_lifecycle`
FOR EACH ROW CALL `phase10_assert_writes_allowed`()$$
DELIMITER ;

-- Application lifecycle timestamps are stored as naive UTC even when the
-- MySQL server/session runs in Asia/Shanghai.
INSERT INTO `system_config`
  (`config_key`, `config_value`, `value_type`, `description`)
VALUES
  ('ttl.job.candidate.days', '7', 'int', '岗位候选版本保留期（天）')
ON DUPLICATE KEY UPDATE `config_key` = VALUES(`config_key`);

SET @phase10_migration_time = UTC_TIMESTAMP();
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

-- Freeze the exact post-migration lifecycle state. Destructive down migration
-- must reject every write that happened after this point instead of overwriting it.
ALTER TABLE `phase10_job_lifecycle_backup`
  ADD COLUMN `expected_audit_status` ENUM('pending','passed','rejected') NULL,
  ADD COLUMN `expected_expires_at` DATETIME NULL,
  ADD COLUMN `expected_deleted_at` DATETIME NULL,
  ADD COLUMN `expected_delist_reason` ENUM('filled','manual_delist','expired','replaced') NULL,
  ADD COLUMN `expected_version` INT UNSIGNED NULL,
  ADD COLUMN `expected_updated_at` DATETIME NULL,
  ADD COLUMN `expected_activated_at` DATETIME NULL,
  ADD COLUMN `expected_candidate_expires_at` DATETIME NULL;

UPDATE `phase10_job_lifecycle_backup` AS b
JOIN `job` AS j ON j.`id` = b.`job_id`
SET b.`expected_audit_status` = b.`audit_status`,
    b.`expected_expires_at` = CASE
      WHEN b.`deleted_at` IS NOT NULL OR b.`audit_status` = 'passed'
      THEN b.`expires_at` ELSE NULL END,
    b.`expected_deleted_at` = b.`deleted_at`,
    b.`expected_delist_reason` = b.`delist_reason`,
    b.`expected_version` = b.`version` + 1,
    b.`expected_updated_at` = j.`updated_at`,
    b.`expected_activated_at` = CASE
      WHEN b.`deleted_at` IS NOT NULL
      THEN COALESCE(b.`source_audited_at`, b.`source_created_at`, b.`source_updated_at`)
      WHEN b.`audit_status` = 'passed'
      THEN COALESCE(b.`source_audited_at`, b.`source_created_at`)
      ELSE NULL END,
    b.`expected_candidate_expires_at` = CASE
      WHEN b.`deleted_at` IS NULL AND b.`audit_status` IN ('pending', 'rejected')
      THEN DATE_ADD(@phase10_migration_time, INTERVAL @phase10_candidate_days DAY)
      ELSE NULL END;

ALTER TABLE `phase10_job_lifecycle_backup`
  DROP COLUMN `source_created_at`,
  DROP COLUMN `source_audited_at`;

ALTER TABLE `phase10_job_lifecycle_backup`
  MODIFY COLUMN `expected_audit_status` ENUM('pending','passed','rejected') NOT NULL,
  MODIFY COLUMN `expected_version` INT UNSIGNED NOT NULL,
  MODIFY COLUMN `expected_updated_at` DATETIME NOT NULL;

UPDATE `phase10_migration_control`
SET `backup_rows` = (SELECT COUNT(*) FROM `phase10_job_lifecycle_backup`),
    `backup_checksum` = (
      SELECT COALESCE(BIT_XOR(CRC32(CONCAT_WS('|', `job_id`, `audit_status`,
        COALESCE(`expires_at`, ''), COALESCE(`deleted_at`, ''),
        COALESCE(`delist_reason`, ''), `version`, `source_updated_at`))), 0)
      FROM `phase10_job_lifecycle_backup`
    ),
    `source_soft_deleted_rows` = (
      SELECT COUNT(*) FROM `phase10_job_lifecycle_backup`
      WHERE `deleted_at` IS NOT NULL
    ),
    `source_passed_online_rows` = (
      SELECT COUNT(*) FROM `phase10_job_lifecycle_backup`
      WHERE `deleted_at` IS NULL AND `audit_status` = 'passed'
    ),
    `source_candidate_rows` = (
      SELECT COUNT(*) FROM `phase10_job_lifecycle_backup`
      WHERE `deleted_at` IS NULL AND `audit_status` IN ('pending', 'rejected')
    ),
    `expected_live_checksum` = (
      SELECT COALESCE(BIT_XOR(CRC32(CONCAT_WS('|', `job_id`, `expected_audit_status`,
        COALESCE(`expected_expires_at`, ''), COALESCE(`expected_deleted_at`, ''),
        COALESCE(`expected_delist_reason`, ''), `expected_version`,
        `expected_updated_at`,
        COALESCE(`expected_activated_at`, ''),
        COALESCE(`expected_candidate_expires_at`, '')))), 0)
      FROM `phase10_job_lifecycle_backup`
    )
WHERE `id` = 1;

ALTER TABLE `phase10_migration_control`
  MODIFY COLUMN `backup_rows` BIGINT UNSIGNED NOT NULL,
  MODIFY COLUMN `backup_checksum` BIGINT UNSIGNED NOT NULL,
  MODIFY COLUMN `source_soft_deleted_rows` BIGINT UNSIGNED NOT NULL,
  MODIFY COLUMN `source_passed_online_rows` BIGINT UNSIGNED NOT NULL,
  MODIFY COLUMN `source_candidate_rows` BIGINT UNSIGNED NOT NULL,
  MODIFY COLUMN `expected_live_checksum` BIGINT UNSIGNED NOT NULL;

DELIMITER $$
-- Fail inside the migration window if any legal-looking row was reclassified or
-- if the post-backfill lifecycle projection differs from its frozen expectation.
CREATE PROCEDURE `phase10_assert_lifecycle_backfill`()
SQL SECURITY INVOKER
BEGIN
  DECLARE migration_mismatch TINYINT DEFAULT 1;
  SELECT CASE WHEN
    c.`backup_rows` <> (SELECT COUNT(*) FROM `phase10_job_lifecycle_backup`)
    OR c.`backup_rows` <> (SELECT COUNT(*) FROM `job`)
    OR c.`backup_rows` <>
       c.`source_soft_deleted_rows` + c.`source_passed_online_rows` + c.`source_candidate_rows`
    OR c.`backup_checksum` <> (
      SELECT COALESCE(BIT_XOR(CRC32(CONCAT_WS('|', `job_id`, `audit_status`,
        COALESCE(`expires_at`, ''), COALESCE(`deleted_at`, ''),
        COALESCE(`delist_reason`, ''), `version`, `source_updated_at`))), 0)
      FROM `phase10_job_lifecycle_backup`
    )
    OR c.`source_soft_deleted_rows` <> (
      SELECT COUNT(*) FROM `phase10_job_lifecycle_backup` WHERE `deleted_at` IS NOT NULL
    )
    OR c.`source_passed_online_rows` <> (
      SELECT COUNT(*) FROM `phase10_job_lifecycle_backup`
      WHERE `deleted_at` IS NULL AND `audit_status` = 'passed'
    )
    OR c.`source_candidate_rows` <> (
      SELECT COUNT(*) FROM `phase10_job_lifecycle_backup`
      WHERE `deleted_at` IS NULL AND `audit_status` IN ('pending', 'rejected')
    )
    OR c.`expected_live_checksum` <> (
      SELECT COALESCE(BIT_XOR(CRC32(CONCAT_WS('|', `job_id`, `expected_audit_status`,
        COALESCE(`expected_expires_at`, ''), COALESCE(`expected_deleted_at`, ''),
        COALESCE(`expected_delist_reason`, ''), `expected_version`,
        `expected_updated_at`,
        COALESCE(`expected_activated_at`, ''),
        COALESCE(`expected_candidate_expires_at`, '')))), 0)
      FROM `phase10_job_lifecycle_backup`
    )
    OR c.`source_soft_deleted_rows` <> (
      SELECT COUNT(*) FROM `job` WHERE `deleted_at` IS NOT NULL
    )
    OR c.`source_passed_online_rows` <> (
      SELECT COUNT(*) FROM `job`
      WHERE `deleted_at` IS NULL AND `audit_status` = 'passed'
    )
    OR c.`source_candidate_rows` <> (
      SELECT COUNT(*) FROM `job`
      WHERE `deleted_at` IS NULL AND `audit_status` IN ('pending', 'rejected')
    )
    OR c.`expected_live_checksum` <> (
      SELECT COALESCE(BIT_XOR(CRC32(CONCAT_WS('|', `id`, `audit_status`,
        COALESCE(`expires_at`, ''), COALESCE(`deleted_at`, ''),
        COALESCE(`delist_reason`, ''), `version`, `updated_at`,
        COALESCE(`activated_at`, ''),
        COALESCE(`candidate_expires_at`, '')))), 0)
      FROM `job`
    )
    THEN 1 ELSE 0 END
  INTO migration_mismatch
  FROM `phase10_migration_control` AS c
  WHERE c.`id` = 1;

  IF migration_mismatch <> 0 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'phase10_lifecycle_backfill_evidence_mismatch';
  END IF;
END$$
DELIMITER ;

CALL `phase10_assert_lifecycle_backfill`();
DROP PROCEDURE `phase10_assert_lifecycle_backfill`;

-- Deployment gate: this query must return zero rows before writes resume.
SELECT `id` FROM `job`
WHERE (`deleted_at` IS NULL AND `audit_status` = 'passed'
       AND (`activated_at` IS NULL OR `expires_at` IS NULL))
   OR (`deleted_at` IS NULL AND `audit_status` IN ('pending', 'rejected')
       AND (`activated_at` IS NOT NULL OR `expires_at` IS NOT NULL
            OR `candidate_expires_at` IS NULL));

-- Deployment gate: AUTO_INCREMENT must remain strictly above every existing Job ID.
SELECT
  t.`AUTO_INCREMENT` AS `next_job_id`,
  COALESCE(MAX(j.`id`), 0) AS `max_job_id`,
  t.`AUTO_INCREMENT` > COALESCE(MAX(j.`id`), 0) AS `auto_increment_valid`
FROM `information_schema`.`TABLES` AS t
LEFT JOIN `job` AS j ON 1=1
WHERE t.`TABLE_SCHEMA` = DATABASE() AND t.`TABLE_NAME` = 'job'
GROUP BY t.`AUTO_INCREMENT`;

-- Classification and checksum evidence for rollout and controlled rollback records.
SELECT
  c.`backup_rows`,
  (SELECT COUNT(*) FROM `job`) AS `job_rows`,
  c.`source_soft_deleted_rows`,
  (SELECT COUNT(*) FROM `job` WHERE `deleted_at` IS NOT NULL) AS `live_soft_deleted_rows`,
  c.`source_passed_online_rows`,
  (SELECT COUNT(*) FROM `job`
   WHERE `deleted_at` IS NULL AND `audit_status` = 'passed') AS `live_passed_online_rows`,
  c.`source_candidate_rows`,
  (SELECT COUNT(*) FROM `job`
   WHERE `deleted_at` IS NULL
     AND `audit_status` IN ('pending', 'rejected')) AS `live_candidate_rows`,
  c.`backup_checksum`,
  c.`expected_live_checksum`,
  (SELECT COALESCE(BIT_XOR(CRC32(CONCAT_WS('|', `id`, `audit_status`,
      COALESCE(`expires_at`, ''), COALESCE(`deleted_at`, ''),
      COALESCE(`delist_reason`, ''), `version`, `updated_at`,
      COALESCE(`activated_at`, ''),
      COALESCE(`candidate_expires_at`, '')))), 0)
   FROM `job`) AS `live_checksum`,
  c.`expected_live_checksum` = (
    SELECT COALESCE(BIT_XOR(CRC32(CONCAT_WS('|', `id`, `audit_status`,
      COALESCE(`expires_at`, ''), COALESCE(`deleted_at`, ''),
      COALESCE(`delist_reason`, ''), `version`, `updated_at`,
      COALESCE(`activated_at`, ''),
      COALESCE(`candidate_expires_at`, '')))), 0)
    FROM `job`
  ) AS `live_checksum_valid`,
  c.`expected_live_checksum` = (
    SELECT COALESCE(BIT_XOR(CRC32(CONCAT_WS('|', `job_id`, `expected_audit_status`,
      COALESCE(`expected_expires_at`, ''), COALESCE(`expected_deleted_at`, ''),
      COALESCE(`expected_delist_reason`, ''), `expected_version`,
      `expected_updated_at`,
      COALESCE(`expected_activated_at`, ''),
      COALESCE(`expected_candidate_expires_at`, '')))), 0)
    FROM `phase10_job_lifecycle_backup`
  ) AS `expected_checksum_valid`
FROM `phase10_migration_control` AS c
WHERE c.`id` = 1;
