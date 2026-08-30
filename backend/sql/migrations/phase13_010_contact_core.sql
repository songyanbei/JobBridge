-- Phase 13 B0: opaque contact request/grant boundary.
-- Additive and idempotent.  Contact values (phone/wechat) are deliberately
-- absent; B1 introduces encrypted source columns separately.

CREATE TABLE IF NOT EXISTS `contact_request` (
  `request_id` VARCHAR(64) NOT NULL,
  `actor_id` VARCHAR(64) NOT NULL,
  `listing_ref` VARCHAR(200) NOT NULL,
  `action` VARCHAR(32) NOT NULL DEFAULT 'request_contact',
  `request_digest` CHAR(64) NOT NULL,
  `nonce_digest` CHAR(64) NOT NULL,
  `listing_version` INT UNSIGNED NULL,
  `policy_version` VARCHAR(64) NULL,
  `status` ENUM('pending','authorized','revoked','expired') NOT NULL DEFAULT 'pending',
  `expires_at` DATETIME(6) NOT NULL,
  `revoked_at` DATETIME(6) NULL,
  `revoke_reason` VARCHAR(64) NULL,
  `trace_id` VARCHAR(64) NULL,
  `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `updated_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`request_id`),
  KEY `idx_contact_request_actor` (`actor_id`,`created_at`,`request_id`),
  KEY `idx_contact_request_listing` (`listing_ref`,`status`,`expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `contact_grant` (
  `grant_id` VARCHAR(64) NOT NULL,
  `request_id` VARCHAR(64) NOT NULL,
  `actor_id` VARCHAR(64) NOT NULL,
  `listing_ref` VARCHAR(200) NOT NULL,
  `action` VARCHAR(32) NOT NULL,
  `token_hash` CHAR(64) NOT NULL,
  `nonce_digest` CHAR(64) NOT NULL,
  `listing_version` INT UNSIGNED NULL,
  `policy_version` VARCHAR(64) NULL,
  `status` ENUM('issued','used','revoked','expired') NOT NULL DEFAULT 'issued',
  `expires_at` DATETIME(6) NOT NULL,
  `used_at` DATETIME(6) NULL,
  `revoked_at` DATETIME(6) NULL,
  `revoke_reason` VARCHAR(64) NULL,
  `trace_id` VARCHAR(64) NULL,
  `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`grant_id`),
  UNIQUE KEY `uk_contact_grant_token_hash` (`token_hash`),
  KEY `idx_contact_grant_actor` (`actor_id`,`created_at`,`grant_id`),
  KEY `idx_contact_grant_due` (`status`,`expires_at`,`grant_id`),
  KEY `idx_contact_grant_request` (`request_id`,`status`),
  CONSTRAINT `fk_contact_grant_request` FOREIGN KEY (`request_id`)
    REFERENCES `contact_request` (`request_id`) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `contact_access_audit` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `event_id` CHAR(36) NOT NULL,
  `event_type` VARCHAR(32) NOT NULL,
  `outcome` VARCHAR(32) NOT NULL,
  `reason_code` VARCHAR(64) NOT NULL,
  `actor_hash` CHAR(64) NULL,
  `listing_hash` CHAR(64) NULL,
  `request_id` VARCHAR(64) NULL,
  `grant_id` VARCHAR(64) NULL,
  `trace_id` VARCHAR(64) NULL,
  `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_contact_audit_event` (`event_id`),
  KEY `idx_contact_audit_trace` (`trace_id`,`created_at`),
  KEY `idx_contact_audit_actor` (`actor_hash`,`created_at`),
  KEY `idx_contact_audit_request` (`request_id`,`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
