-- DESTRUCTIVE DOWN. Runner also requires confirmation, build probes and cutover watermark.
-- Keep every guard self-contained.  The runner checkpoints statements after
-- commit, so a session variable set by an earlier statement is not durable
-- resume state and must never be needed by a later process.
CREATE TABLE IF NOT EXISTS `phase11_down_guard` (`ok` TINYINT NOT NULL CHECK (`ok`=1));
DELETE FROM `phase11_down_guard`;
INSERT INTO `phase11_down_guard` SELECT IF(
   (SELECT COUNT(*) FROM `resume_replacement` WHERE `lifecycle_status` IN ('awaiting_review','conflict'))
 + (SELECT COUNT(*) FROM `target_cleanup_task` WHERE `target_type`='resume' AND `status` <> 'succeeded')
 + (SELECT COUNT(*) FROM `resume_media_isolation_issue` WHERE `status` <> 'resolved')
 + (SELECT COUNT(*) FROM `phase11_migration_ledger` WHERE `migration_key` <> 'phase11_resume_lifecycle_down' AND `status` NOT IN ('succeeded','verified'))
 + (SELECT COUNT(*) FROM `resume` r
      LEFT JOIN `phase11_resume_lifecycle_backup` b ON b.`resume_id`=r.`id`
      WHERE r.`expires_at` IS NULL
        AND NOT (r.`audit_status` IN ('pending','rejected') AND r.`activated_at` IS NULL
          AND (r.`id` > COALESCE((SELECT `cutover_resume_id` FROM `phase11_migration_ledger`
              WHERE `migration_key`='phase11_resume_lifecycle_backfill'),0) OR b.`resume_id` IS NOT NULL)))
=0,1,0);
DROP TABLE `phase11_down_guard`;

-- Full Resume image, all business-history tables, and a point-in-time ledger
-- copy remain after down as retention-controlled audit evidence.
CREATE TABLE IF NOT EXISTS `phase11_resume_down_backup` LIKE `resume`;
DELETE FROM `phase11_resume_down_backup`;
REPLACE INTO `phase11_resume_down_backup` SELECT * FROM `resume`;
CREATE TABLE IF NOT EXISTS `phase11_resume_down_replacement_backup` LIKE `resume_replacement`;
DELETE FROM `phase11_resume_down_replacement_backup`;
REPLACE INTO `phase11_resume_down_replacement_backup` SELECT * FROM `resume_replacement`;
CREATE TABLE IF NOT EXISTS `phase11_resume_down_assignment_backup` LIKE `resume_replacement_rollout_assignment`;
DELETE FROM `phase11_resume_down_assignment_backup`;
REPLACE INTO `phase11_resume_down_assignment_backup` SELECT * FROM `resume_replacement_rollout_assignment`;
CREATE TABLE IF NOT EXISTS `phase11_resume_down_media_issue_backup` LIKE `resume_media_isolation_issue`;
DELETE FROM `phase11_resume_down_media_issue_backup`;
REPLACE INTO `phase11_resume_down_media_issue_backup` SELECT * FROM `resume_media_isolation_issue`;
CREATE TABLE IF NOT EXISTS `phase11_resume_down_ledger_backup` LIKE `phase11_migration_ledger`;
DELETE FROM `phase11_resume_down_ledger_backup`;
REPLACE INTO `phase11_resume_down_ledger_backup` SELECT * FROM `phase11_migration_ledger`;
DELETE FROM `phase11_resume_down_ledger_backup` WHERE `migration_key`='phase11_resume_lifecycle_down';

CREATE TABLE IF NOT EXISTS `phase11_resume_down_export_audit` (
 `artifact_name` VARCHAR(64) NOT NULL, `row_count` BIGINT UNSIGNED NOT NULL,
 `row_digest` CHAR(64) NOT NULL, `captured_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
 PRIMARY KEY (`artifact_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
-- The runner recognizes this pinned marker and streams a canonical ordered
-- SHA-256 chain over every column of every source/backup row.  Capturing all
-- ten rows is one transaction, so replay after commit-before-ordinal is safe.
SELECT 'phase11_capture_down_export_audits';

CREATE TABLE IF NOT EXISTS `phase11_export_guard` (`ok` TINYINT NOT NULL CHECK (`ok`=1));
DELETE FROM `phase11_export_guard`;
INSERT INTO `phase11_export_guard` SELECT IF(
 (SELECT ABS((SELECT COUNT(*) FROM `resume`)-(SELECT COUNT(*) FROM `phase11_resume_down_backup`)))
 + (SELECT ABS((SELECT COUNT(*) FROM `resume_replacement`)-(SELECT COUNT(*) FROM `phase11_resume_down_replacement_backup`)))
 + (SELECT ABS((SELECT COUNT(*) FROM `resume_replacement_rollout_assignment`)-(SELECT COUNT(*) FROM `phase11_resume_down_assignment_backup`)))
 + (SELECT ABS((SELECT COUNT(*) FROM `resume_media_isolation_issue`)-(SELECT COUNT(*) FROM `phase11_resume_down_media_issue_backup`)))
 + (SELECT ABS((SELECT COUNT(*) FROM `phase11_migration_ledger`
      WHERE `migration_key`<>'phase11_resume_lifecycle_down')
    -(SELECT COUNT(*) FROM `phase11_resume_down_ledger_backup`
      WHERE `migration_key`<>'phase11_resume_lifecycle_down')))
 + (SELECT IF(COUNT(*)=10,0,1) FROM `phase11_resume_down_export_audit`)
 + (SELECT IF(COUNT(*)=10,0,1) FROM `phase11_resume_down_export_audit`
    WHERE `artifact_name` IN ('source_resume','backup_resume','source_replacement','backup_replacement',
      'source_assignment','backup_assignment','source_media_issue','backup_media_issue','source_ledger','backup_ledger'))
 + (SELECT COUNT(*) FROM `phase11_resume_down_export_audit`
    WHERE `artifact_name` NOT IN ('source_resume','backup_resume','source_replacement','backup_replacement',
      'source_assignment','backup_assignment','source_media_issue','backup_media_issue','source_ledger','backup_ledger'))
 + (SELECT COUNT(*) FROM (SELECT SUBSTRING(artifact_name,8) AS name,row_count,row_digest
     FROM phase11_resume_down_export_audit WHERE LEFT(artifact_name,7)='source_') s
     JOIN (SELECT SUBSTRING(artifact_name,8) AS name,row_count,row_digest
       FROM phase11_resume_down_export_audit WHERE LEFT(artifact_name,7)='backup_') b USING(name)
     WHERE s.row_count<>b.row_count OR s.row_digest<>b.row_digest)
=0,1,0);
DROP TABLE `phase11_export_guard`;

-- Restore original TTL only when current TTL is NULL; retain valid new values.
UPDATE `resume` r JOIN `phase11_resume_lifecycle_backup` b ON b.`resume_id`=r.`id`
SET r.`expires_at`=b.`expires_at`,r.`updated_at`=r.`updated_at`
WHERE r.`expires_at` IS NULL AND b.`expires_at` IS NOT NULL;
-- Deterministic mapping for live and already-soft-deleted candidates. Never
-- mutate deleted_at, and never physically delete a candidate during down.
UPDATE `resume` r JOIN `system_config` c ON c.`config_key`='ttl.resume.days'
SET r.`expires_at`=COALESCE(r.`candidate_expires_at`,r.`created_at` + INTERVAL CAST(c.`config_value` AS UNSIGNED) DAY),
    r.`updated_at`=r.`updated_at`
WHERE r.`expires_at` IS NULL AND r.`audit_status` IN ('pending','rejected') AND r.`activated_at` IS NULL;

CREATE TABLE IF NOT EXISTS `phase11_null_ttl_guard` (`ok` TINYINT NOT NULL CHECK (`ok`=1));
DELETE FROM `phase11_null_ttl_guard`;
INSERT INTO `phase11_null_ttl_guard`
SELECT IF((SELECT COUNT(*) FROM `resume` WHERE `expires_at` IS NULL)=0,1,0);
DROP TABLE `phase11_null_ttl_guard`;

DROP TABLE `resume_replacement`;
DROP TABLE `resume_replacement_rollout_assignment`;
DROP TABLE `resume_media_isolation_issue`;
ALTER TABLE `resume` DROP KEY `idx_resume_hard_delete`;
ALTER TABLE `resume` DROP KEY `idx_resume_candidate_expiry`;
ALTER TABLE `resume` DROP COLUMN `candidate_expires_at`;
ALTER TABLE `resume` DROP COLUMN `activated_at`;
ALTER TABLE `resume` DROP COLUMN `delist_reason`;
ALTER TABLE `resume` MODIFY COLUMN `expires_at` DATETIME(6) NOT NULL;
