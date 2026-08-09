-- Destructive rollback is permitted only before phase10 creates/backfills model data.
-- Stop all job writes and disable Job hard delete before executing this script.
SET @phase10_down_blocked = (
  EXISTS (SELECT 1 FROM `job_replacement` LIMIT 1)
  OR EXISTS (SELECT 1 FROM `target_cleanup_task` LIMIT 1)
  OR EXISTS (SELECT 1 FROM `media_asset_lifecycle` LIMIT 1)
  OR EXISTS (
    SELECT 1 FROM `job` j
    LEFT JOIN `phase10_job_lifecycle_backup` b ON b.`job_id` = j.`id`
    WHERE b.`job_id` IS NULL LIMIT 1
  )
  OR (SELECT COUNT(*) FROM `job`) <> (SELECT COUNT(*) FROM `phase10_job_lifecycle_backup`)
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
  SELECT BIT_XOR(CRC32(CONCAT_WS('|', `job_id`, `audit_status`,
    COALESCE(`expires_at`, ''), COALESCE(`deleted_at`, ''),
    COALESCE(`delist_reason`, ''), `version`)))
  FROM `phase10_job_lifecycle_backup`
);
SET @phase10_restored_checksum = (
  SELECT BIT_XOR(CRC32(CONCAT_WS('|', `id`, `audit_status`,
    COALESCE(`expires_at`, ''), COALESCE(`deleted_at`, ''),
    COALESCE(`delist_reason`, ''), `version`)))
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

ALTER TABLE `job` DROP INDEX `idx_job_candidate_expiry`;
ALTER TABLE `job` DROP COLUMN `candidate_expires_at`, DROP COLUMN `activated_at`;
ALTER TABLE `job` MODIFY COLUMN `expires_at` DATETIME NOT NULL;
ALTER TABLE `job` MODIFY COLUMN `delist_reason` ENUM('filled','manual_delist','expired') NULL;
DROP TABLE `job_replacement`;
DROP TABLE `media_asset_lifecycle`;
DROP TABLE `target_cleanup_task`;
