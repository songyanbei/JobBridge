-- Phase 14 consumer lease/fencing and retry metadata (additive).
ALTER TABLE `domain_outbox_event`
  ADD COLUMN `attempt_count` INT UNSIGNED NOT NULL DEFAULT 0,
  ADD COLUMN `next_attempt_at` DATETIME(6) NULL,
  ADD COLUMN `lease_owner` VARCHAR(64) NULL,
  ADD COLUMN `lease_until` DATETIME(6) NULL,
  ADD COLUMN `fencing_token` BIGINT UNSIGNED NOT NULL DEFAULT 0,
  ADD COLUMN `last_error` VARCHAR(255) NULL,
  ADD KEY `idx_domain_outbox_claim` (`status`,`next_attempt_at`,`lease_until`,`occurred_at`,`id`);
