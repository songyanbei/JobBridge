-- Phase 14 additive migration: versioned Job aggregate + domain outbox.
-- Apply after a preflight confirms no conflicting columns/indexes. Down migration
-- is intentionally non-destructive: stop consumers and retain events/facts.
ALTER TABLE `job`
  ADD COLUMN `aggregate_version` BIGINT UNSIGNED NOT NULL DEFAULT 1
  COMMENT '领域聚合版本号';

UPDATE `job` SET `aggregate_version` = GREATEST(COALESCE(`version`, 1), 1)
  WHERE `aggregate_version` IS NULL OR `aggregate_version` < 1;

CREATE TABLE `domain_outbox_event` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `aggregate_type` VARCHAR(32) NOT NULL,
  `aggregate_id` BIGINT UNSIGNED NOT NULL,
  `aggregate_version` BIGINT UNSIGNED NOT NULL,
  `event_type` VARCHAR(64) NOT NULL,
  `payload` JSON NOT NULL,
  `payload_digest` CHAR(64) NOT NULL,
  `trace_id` VARCHAR(64) DEFAULT NULL,
  `occurred_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `tombstone` TINYINT(1) NOT NULL DEFAULT 0,
  `status` ENUM('pending','processing','published','dead_letter') NOT NULL DEFAULT 'pending',
  `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_domain_outbox_versioned_event` (`aggregate_type`,`aggregate_id`,`aggregate_version`,`event_type`),
  KEY `idx_domain_outbox_pending` (`status`,`occurred_at`,`id`),
  KEY `idx_domain_outbox_aggregate` (`aggregate_type`,`aggregate_id`,`aggregate_version`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
