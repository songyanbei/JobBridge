-- Destructive rollback is permitted only before phase10 creates model data.
-- Stop all job writes before executing this script.
SELECT CASE
  WHEN EXISTS (SELECT 1 FROM `job_replacement` LIMIT 1)
    OR EXISTS (SELECT 1 FROM `job` WHERE `candidate_expires_at` IS NOT NULL LIMIT 1)
  THEN (SELECT 1 / 0)
  ELSE 1
END AS `phase10_down_precondition`;

UPDATE `job` AS j
JOIN `phase10_job_lifecycle_backup` AS b ON b.`job_id` = j.`id`
SET j.`audit_status` = b.`audit_status`,
    j.`expires_at` = b.`expires_at`,
    j.`deleted_at` = b.`deleted_at`,
    j.`delist_reason` = b.`delist_reason`,
    j.`version` = b.`version`;

ALTER TABLE `job` DROP INDEX `idx_job_candidate_expiry`;
ALTER TABLE `job` DROP COLUMN `candidate_expires_at`, DROP COLUMN `activated_at`;
ALTER TABLE `job` MODIFY COLUMN `expires_at` DATETIME NOT NULL;
ALTER TABLE `job` MODIFY COLUMN `delist_reason` ENUM('filled','manual_delist','expired') NULL;
DROP TABLE `job_replacement`;
