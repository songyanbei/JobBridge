-- Phase 13 B2: stable delivery reference for one-time Contact grants.
-- Additive/idempotent; the outbox stores only this opaque reference.

CREATE TABLE IF NOT EXISTS `contact_delivery` (
  `delivery_id` VARCHAR(64) NOT NULL,
  `grant_id` VARCHAR(64) NOT NULL,
  `actor_id` VARCHAR(64) NOT NULL,
  `listing_ref` VARCHAR(200) NOT NULL,
  `channel` VARCHAR(32) NOT NULL DEFAULT 'platform_request',
  `content_ciphertext` VARBINARY(4096) NULL,
  `key_version` SMALLINT UNSIGNED NULL,
  `content_hash` CHAR(64) NULL,
  `status` ENUM('prepared','sending','sent','retry_wait','revoked','expired') NOT NULL DEFAULT 'prepared',
  `expires_at` DATETIME(6) NOT NULL,
  `revoked_at` DATETIME(6) NULL,
  `revoke_reason` VARCHAR(64) NULL,
  `sent_at` DATETIME(6) NULL,
  `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`delivery_id`),
  UNIQUE KEY `uk_contact_delivery_grant` (`grant_id`),
  KEY `idx_contact_delivery_due` (`status`,`expires_at`,`delivery_id`),
  KEY `idx_contact_delivery_actor` (`actor_id`,`created_at`,`delivery_id`),
  CONSTRAINT `fk_contact_delivery_grant` FOREIGN KEY (`grant_id`)
    REFERENCES `contact_grant` (`grant_id`) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

SET @db := DATABASE();
SET @sql := (SELECT IF(COUNT(*) = 0,
  'ALTER TABLE `wecom_outbound_outbox` ADD COLUMN `contact_delivery_id` VARCHAR(64) NULL',
  'SELECT 1') FROM information_schema.columns WHERE table_schema=@db AND table_name='wecom_outbound_outbox' AND column_name='contact_delivery_id');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @sql := (SELECT IF(COUNT(*) = 0,
  'ALTER TABLE `wecom_outbound_outbox` ADD UNIQUE KEY `uk_outbox_contact_delivery` (`contact_delivery_id`)',
  'SELECT 1') FROM information_schema.statistics WHERE table_schema=@db AND table_name='wecom_outbound_outbox' AND index_name='uk_outbox_contact_delivery');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @sql := (SELECT IF(COUNT(*) = 0,
  'ALTER TABLE `wecom_outbound_outbox` ADD KEY `idx_outbox_contact_delivery` (`status`,`contact_delivery_id`,`id`)',
  'SELECT 1') FROM information_schema.statistics WHERE table_schema=@db AND table_name='wecom_outbound_outbox' AND index_name='idx_outbox_contact_delivery');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
-- MySQL 8 rejects a CHECK that references a column participating in a
-- foreign-key referential action (ERROR 3823).  Use native triggers instead;
-- this also converges cleanly when the earlier CREATE/ALTER statements were
-- committed before a prior run failed at the CHECK.  Trigger DDL must not use
-- PREPARE (ERROR 1295), so the two migration-owned triggers are rebuilt.
SET @sql := (SELECT IF(COUNT(*) = 0,
  'SELECT 1',
  'ALTER TABLE `wecom_outbound_outbox` DROP CHECK `ck_outbox_single_delivery_kind`')
  FROM information_schema.table_constraints
  WHERE table_schema=@db AND table_name='wecom_outbound_outbox'
    AND constraint_name='ck_outbox_single_delivery_kind');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

DROP TRIGGER IF EXISTS `trg_outbox_single_delivery_kind_ins`;
DROP TRIGGER IF EXISTS `trg_outbox_single_delivery_kind_upd`;

DELIMITER $$
CREATE TRIGGER `trg_outbox_single_delivery_kind_ins`
BEFORE INSERT ON `wecom_outbound_outbox`
FOR EACH ROW
BEGIN
  IF NEW.`recommendation_delivery_id` IS NOT NULL
     AND NEW.`contact_delivery_id` IS NOT NULL THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'outbox delivery kind conflict';
  END IF;
END$$

CREATE TRIGGER `trg_outbox_single_delivery_kind_upd`
BEFORE UPDATE ON `wecom_outbound_outbox`
FOR EACH ROW
BEGIN
  IF NEW.`recommendation_delivery_id` IS NOT NULL
     AND NEW.`contact_delivery_id` IS NOT NULL THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'outbox delivery kind conflict';
  END IF;
END$$
DELIMITER ;
