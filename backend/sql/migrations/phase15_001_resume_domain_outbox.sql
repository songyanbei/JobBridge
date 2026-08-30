-- Phase 15 additive migration: Resume aggregate version and versioned events.
-- Safe rollout: schema first, then keyset backfill, then verify. No legacy
-- columns or rows are removed; consumers may be stopped and facts retained.
ALTER TABLE `resume`
  ADD COLUMN `aggregate_version` BIGINT UNSIGNED NOT NULL DEFAULT 1
  COMMENT '领域聚合版本号';

UPDATE `resume`
SET `aggregate_version` = GREATEST(COALESCE(`version`, 1), 1)
WHERE `aggregate_version` IS NULL OR `aggregate_version` < 1;

ALTER TABLE `resume`
  ADD KEY `idx_resume_aggregate_version` (`id`, `aggregate_version`),
  ADD KEY `idx_resume_online_version` (`audit_status`, `deleted_at`, `delist_reason`, `expires_at`, `aggregate_version`);

-- The shared domain_outbox_event table is created by Phase 14. Resume events
-- use aggregate_type='resume' and the same uniqueness/tombstone contract.
