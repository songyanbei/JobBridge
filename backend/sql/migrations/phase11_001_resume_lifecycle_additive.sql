-- Phase 11 pre-cutover additive schema. No backfill or destructive operation.
CREATE TABLE IF NOT EXISTS `phase11_migration_ledger` (
 `migration_key` VARCHAR(128) NOT NULL, `script_sha256` CHAR(64) NOT NULL,
 `stage` ENUM('pre_cutover','post_cutover','verify','down') NOT NULL,
 `kind` ENUM('sql','python','verify_sql') NOT NULL,
 `status` ENUM('running','succeeded','failed','verified') NOT NULL,
 `attempt` INT UNSIGNED NOT NULL DEFAULT 0, `last_statement_ordinal` INT UNSIGNED NOT NULL DEFAULT 0,
 `resume_cursor_json` JSON NULL, `started_at` DATETIME(6) NULL, `completed_at` DATETIME(6) NULL,
 `cutover_resume_id` BIGINT UNSIGNED NULL, `build_probe_digest` CHAR(64) NULL,
 `executed_by` VARCHAR(128) NOT NULL, `error_code` VARCHAR(64) NULL,
 `verification_digest` CHAR(64) NULL,
 `updated_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
 PRIMARY KEY (`migration_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

ALTER TABLE `resume` ADD COLUMN `activated_at` DATETIME(6) NULL COMMENT '业务激活时间';
ALTER TABLE `resume` ADD COLUMN `candidate_expires_at` DATETIME(6) NULL COMMENT '候选版本回收时间';
ALTER TABLE `resume` MODIFY COLUMN `expires_at` DATETIME(6) NULL COMMENT '激活后的业务过期时间';
ALTER TABLE `resume` ADD COLUMN `delist_reason` VARCHAR(32) NULL COMMENT '下架原因';
ALTER TABLE `resume` MODIFY COLUMN `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6);
ALTER TABLE `resume` MODIFY COLUMN `updated_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6);
ALTER TABLE `resume` MODIFY COLUMN `deleted_at` DATETIME(6) NULL;
ALTER TABLE `resume` ADD KEY `idx_resume_candidate_expiry` (`audit_status`,`candidate_expires_at`);
ALTER TABLE `resume` ADD KEY `idx_resume_hard_delete` (`deleted_at`,`id`);

CREATE TABLE IF NOT EXISTS `resume_replacement` (
 `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT, `operation_id` CHAR(36) NOT NULL,
 `source_msg_id` VARCHAR(128) NOT NULL, `owner_userid` VARCHAR(64) NOT NULL,
 `old_resume_id` BIGINT UNSIGNED NOT NULL, `new_resume_id` BIGINT UNSIGNED NOT NULL,
 `old_resume_version` INT UNSIGNED NOT NULL, `old_expires_at` DATETIME(6) NULL,
 `old_business_digest` CHAR(64) NOT NULL, `old_business_digest_version` TINYINT UNSIGNED NOT NULL DEFAULT 2,
 `review_outcome` ENUM('pending','passed','rejected') NOT NULL DEFAULT 'pending',
 `reviewed_at` DATETIME(6) NULL, `reviewed_by` VARCHAR(64) NULL,
 `lifecycle_status` ENUM('awaiting_review','activated','closed','conflict') NOT NULL DEFAULT 'awaiting_review',
 `active_old_resume_id` BIGINT UNSIGNED NULL, `closed_reason` VARCHAR(64) NULL,
 `conflict_reason` VARCHAR(255) NULL, `activated_at` DATETIME(6) NULL,
 `candidate_cleaned_at` DATETIME(6) NULL, `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
 `updated_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
 PRIMARY KEY (`id`), UNIQUE KEY `uq_resume_replacement_operation` (`operation_id`),
 UNIQUE KEY `uq_resume_replacement_message` (`source_msg_id`),
 UNIQUE KEY `uq_resume_replacement_new` (`new_resume_id`),
 UNIQUE KEY `uq_resume_replacement_active_old` (`active_old_resume_id`),
 KEY `idx_resume_replacement_old_status` (`old_resume_id`,`lifecycle_status`),
 KEY `idx_resume_replacement_owner_created` (`owner_userid`,`created_at`),
 KEY `idx_resume_replacement_lifecycle_created` (`lifecycle_status`,`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `resume_replacement_rollout_assignment` (
 `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT, `operation_id` CHAR(36) NOT NULL,
 `owner_userid` VARCHAR(64) NOT NULL, `cohort` ENUM('enabled','control') NOT NULL,
 `allowlist_revision` BIGINT UNSIGNED NOT NULL, `source_msg_id` VARCHAR(128) NOT NULL,
 `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6), PRIMARY KEY (`id`),
 UNIQUE KEY `uq_resume_rollout_operation` (`operation_id`),
 UNIQUE KEY `uq_resume_rollout_message` (`source_msg_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `resume_media_isolation_issue` (
 `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT, `resume_id` BIGINT UNSIGNED NULL,
 `key_hash` CHAR(64) NOT NULL, `issue_type` VARCHAR(64) NOT NULL,
 `status` ENUM('open','approved','resolved','blocked') NOT NULL DEFAULT 'open',
 `disposition` ENUM('assign_owner','detach_reference','delete_object') NULL,
 `approval_reason` VARCHAR(255) NULL, `approved_by` VARCHAR(64) NULL, `approved_at` DATETIME(6) NULL,
 `executed_by` VARCHAR(64) NULL, `executed_at` DATETIME(6) NULL, `resolved_at` DATETIME(6) NULL,
 `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
 `updated_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
 PRIMARY KEY (`id`), UNIQUE KEY `uq_resume_media_isolation_issue` (`resume_id`,`key_hash`,`issue_type`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Hash-only scan registry.  It deliberately stores neither an object key nor
-- an owner identifier and makes media reconciliation restart-safe.
CREATE TABLE IF NOT EXISTS `phase11_resume_media_key_scan` (
 `resume_id` BIGINT UNSIGNED NOT NULL, `key_hash` CHAR(64) NOT NULL,
 `reference_kind` ENUM('valid','invalid') NOT NULL, `reference_count` INT UNSIGNED NOT NULL,
 `first_seen_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
 PRIMARY KEY (`resume_id`,`key_hash`,`reference_kind`),
 KEY `idx_phase11_media_scan_key` (`key_hash`,`reference_kind`,`resume_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `phase11_resume_lifecycle_backup` (
 `resume_id` BIGINT UNSIGNED NOT NULL, `expires_at` DATETIME(6) NULL,
 `activated_at` DATETIME(6) NULL, `candidate_expires_at` DATETIME(6) NULL,
 `deleted_at` DATETIME(6) NULL, `version` INT UNSIGNED NOT NULL, `updated_at` DATETIME(6) NOT NULL,
 `captured_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6), PRIMARY KEY (`resume_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
