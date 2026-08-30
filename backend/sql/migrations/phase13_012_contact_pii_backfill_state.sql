-- Phase 13 B3: resumable PII backfill checkpoints.  No destructive cleanup.
CREATE TABLE IF NOT EXISTS `contact_pii_migration_state` (
  `entity` VARCHAR(16) NOT NULL,
  `last_pk` VARCHAR(128) NOT NULL DEFAULT '0',
  `success_count` BIGINT UNSIGNED NOT NULL DEFAULT 0,
  `error_count` BIGINT UNSIGNED NOT NULL DEFAULT 0,
  `key_version` SMALLINT UNSIGNED NULL,
  `status` ENUM('pending','running','paused','completed') NOT NULL DEFAULT 'pending',
  `last_error_code` VARCHAR(64) NULL,
  `updated_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`entity`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT INTO `contact_pii_migration_state` (`entity`,`status`)
VALUES ('user','pending'),('job','pending')
ON DUPLICATE KEY UPDATE `entity`=`entity`;
