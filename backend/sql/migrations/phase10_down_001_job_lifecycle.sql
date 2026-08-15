-- Destructive rollback is permitted only before phase10 creates/backfills model data.
-- Stop all job writes and disable Job hard delete before executing this script.
SET TRANSACTION ISOLATION LEVEL REPEATABLE READ;
SET @phase10_destructive_down_authorized = 1;
START TRANSACTION;
UPDATE `phase10_migration_control` SET `writes_blocked` = 1 WHERE `id` = 1;
SELECT COUNT(*) AS `phase10_locked_job_rows` FROM `job` FOR UPDATE;
SELECT COUNT(*) AS `phase10_locked_backup_rows`
FROM `phase10_job_lifecycle_backup` FOR SHARE;
SELECT COUNT(*) AS `phase10_locked_replacement_rows` FROM `job_replacement` FOR UPDATE;
SELECT COUNT(*) AS `phase10_locked_cleanup_rows` FROM `target_cleanup_task` FOR UPDATE;
SELECT COUNT(*) AS `phase10_locked_media_rows` FROM `media_asset_lifecycle` FOR UPDATE;
SELECT `id` AS `phase10_locked_session_pending_id`
FROM `wecom_inbound_event`
WHERE `status` = 'session_pending'
   OR `session_commit_deadline_epoch` IS NOT NULL
   OR `session_apply_lease_owner` IS NOT NULL
FOR UPDATE;

SET @phase10_current_backup_rows = (
  SELECT COUNT(*) FROM `phase10_job_lifecycle_backup`
);
SET @phase10_current_backup_checksum = (
  SELECT COALESCE(BIT_XOR(CRC32(CONCAT_WS('|', `job_id`, `audit_status`,
    COALESCE(`expires_at`, ''), COALESCE(`deleted_at`, ''),
    COALESCE(`delist_reason`, ''), `version`))), 0)
  FROM `phase10_job_lifecycle_backup`
);
SET @phase10_current_expected_live_checksum = (
  SELECT COALESCE(BIT_XOR(CRC32(CONCAT_WS('|', `job_id`, `expected_audit_status`,
    COALESCE(`expected_expires_at`, ''), COALESCE(`expected_deleted_at`, ''),
    COALESCE(`expected_delist_reason`, ''), `expected_version`,
    COALESCE(`expected_activated_at`, ''),
    COALESCE(`expected_candidate_expires_at`, '')))), 0)
  FROM `phase10_job_lifecycle_backup`
);

SET @phase10_down_blocked = (
  @phase10_archived_backup_rows IS NULL
  OR @phase10_archived_backup_checksum IS NULL
  OR @phase10_archived_expected_live_checksum IS NULL
  OR NOT (@phase10_archived_backup_rows <=> @phase10_current_backup_rows)
  OR NOT (@phase10_archived_backup_checksum <=> @phase10_current_backup_checksum)
  OR NOT (
    @phase10_archived_expected_live_checksum
    <=> @phase10_current_expected_live_checksum
  )
  OR
  EXISTS (SELECT 1 FROM `job_replacement` LIMIT 1)
  OR EXISTS (SELECT 1 FROM `target_cleanup_task` LIMIT 1)
  OR EXISTS (SELECT 1 FROM `media_asset_lifecycle` LIMIT 1)
  OR EXISTS (
    SELECT 1 FROM `wecom_inbound_event`
    WHERE `status` = 'session_pending'
       OR `session_commit_deadline_epoch` IS NOT NULL
       OR `session_apply_lease_owner` IS NOT NULL
    LIMIT 1
  )
  OR EXISTS (
    SELECT 1 FROM `job` j
    LEFT JOIN `phase10_job_lifecycle_backup` b ON b.`job_id` = j.`id`
    WHERE b.`job_id` IS NULL LIMIT 1
  )
  OR (SELECT COUNT(*) FROM `job`) <> (SELECT COUNT(*) FROM `phase10_job_lifecycle_backup`)
  OR EXISTS (
    SELECT 1
    FROM `job` j
    JOIN `phase10_job_lifecycle_backup` b ON b.`job_id` = j.`id`
    WHERE NOT (
      j.`audit_status` <=> b.`expected_audit_status`
      AND j.`expires_at` <=> b.`expected_expires_at`
      AND j.`deleted_at` <=> b.`expected_deleted_at`
      AND j.`delist_reason` <=> b.`expected_delist_reason`
      AND j.`version` <=> b.`expected_version`
      AND j.`activated_at` <=> b.`expected_activated_at`
      AND j.`candidate_expires_at` <=> b.`expected_candidate_expires_at`
    )
    LIMIT 1
  )
);
SET @phase10_guard_sql = IF(
  @phase10_down_blocked,
  'SELECT * FROM `phase10_down_guard_failed_new_model_data_exists`',
  'SELECT 1 AS phase10_down_precondition'
);
PREPARE phase10_guard_stmt FROM @phase10_guard_sql;
EXECUTE phase10_guard_stmt;
DEALLOCATE PREPARE phase10_guard_stmt;

UPDATE `job` AS j
JOIN `phase10_job_lifecycle_backup` AS b ON b.`job_id` = j.`id`
SET j.`audit_status` = b.`audit_status`,
    j.`expires_at` = b.`expires_at`,
    j.`deleted_at` = b.`deleted_at`,
    j.`delist_reason` = b.`delist_reason`,
    j.`version` = b.`version`;

SET @phase10_backup_checksum = (
  SELECT COALESCE(BIT_XOR(CRC32(CONCAT_WS('|', `job_id`, `audit_status`,
    COALESCE(`expires_at`, ''), COALESCE(`deleted_at`, ''),
    COALESCE(`delist_reason`, ''), `version`))), 0)
  FROM `phase10_job_lifecycle_backup`
);
SET @phase10_restored_checksum = (
  SELECT COALESCE(BIT_XOR(CRC32(CONCAT_WS('|', `id`, `audit_status`,
    COALESCE(`expires_at`, ''), COALESCE(`deleted_at`, ''),
    COALESCE(`delist_reason`, ''), `version`))), 0)
  FROM `job`
);
SET @phase10_checksum_sql = IF(
  @phase10_backup_checksum <=> @phase10_restored_checksum,
  'SELECT 1 AS phase10_restore_checksum_valid',
  'SELECT * FROM `phase10_down_guard_failed_checksum_mismatch`'
);
PREPARE phase10_checksum_stmt FROM @phase10_checksum_sql;
EXECUTE phase10_checksum_stmt;
DEALLOCATE PREPARE phase10_checksum_stmt;
COMMIT;

ALTER TABLE `wecom_inbound_event`
  DROP COLUMN `session_apply_lease_owner`,
  DROP COLUMN `session_commit_deadline_epoch`;
ALTER TABLE `job` DROP INDEX `idx_job_candidate_expiry`;
ALTER TABLE `job` DROP COLUMN `candidate_expires_at`, DROP COLUMN `activated_at`;
ALTER TABLE `job` MODIFY COLUMN `expires_at` DATETIME NOT NULL;
ALTER TABLE `job` MODIFY COLUMN `delist_reason` ENUM('filled','manual_delist','expired') NULL;
DROP TABLE `job_replacement`;
DROP TABLE `media_asset_lifecycle`;
DROP TABLE `target_cleanup_task`;
DROP TRIGGER `phase10_job_insert_fence`;
DROP TRIGGER `phase10_job_update_fence`;
DROP TRIGGER `phase10_job_delete_fence`;
DROP TRIGGER `phase10_inbound_insert_fence`;
DROP TRIGGER `phase10_inbound_update_fence`;
DROP TRIGGER `phase10_inbound_delete_fence`;
DROP PROCEDURE `phase10_assert_writes_allowed`;
DROP TABLE `phase10_migration_control`;
ALTER TABLE `phase10_job_lifecycle_backup`
  DROP COLUMN `expected_audit_status`,
  DROP COLUMN `expected_expires_at`,
  DROP COLUMN `expected_deleted_at`,
  DROP COLUMN `expected_delist_reason`,
  DROP COLUMN `expected_version`,
  DROP COLUMN `expected_activated_at`,
  DROP COLUMN `expected_candidate_expires_at`;
