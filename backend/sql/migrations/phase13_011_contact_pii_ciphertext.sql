-- Phase 13 B1: additive ciphertext/digest columns. Legacy columns remain
-- readable only to the migration worker until B3 freeze/cleanup approval.

SET @db := DATABASE();

SET @sql := (SELECT IF(COUNT(*) = 0,
  'ALTER TABLE `user` ADD COLUMN `phone_ciphertext` VARBINARY(1024) NULL, ADD COLUMN `phone_key_version` SMALLINT UNSIGNED NULL, ADD COLUMN `phone_digest` CHAR(64) NULL, ADD COLUMN `contact_person_ciphertext` VARBINARY(1024) NULL, ADD COLUMN `contact_person_key_version` SMALLINT UNSIGNED NULL, ADD COLUMN `contact_person_digest` CHAR(64) NULL, ADD COLUMN `wechat_ciphertext` VARBINARY(1024) NULL, ADD COLUMN `wechat_key_version` SMALLINT UNSIGNED NULL, ADD COLUMN `wechat_digest` CHAR(64) NULL',
  'SELECT 1') FROM information_schema.columns WHERE table_schema=@db AND table_name='user' AND column_name='phone_ciphertext');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql := (SELECT IF(COUNT(*) = 0,
  'ALTER TABLE `job` ADD COLUMN `phone_ciphertext` VARBINARY(1024) NULL, ADD COLUMN `phone_key_version` SMALLINT UNSIGNED NULL, ADD COLUMN `phone_digest` CHAR(64) NULL, ADD COLUMN `contact_person_ciphertext` VARBINARY(1024) NULL, ADD COLUMN `contact_person_key_version` SMALLINT UNSIGNED NULL, ADD COLUMN `contact_person_digest` CHAR(64) NULL, ADD COLUMN `wechat_ciphertext` VARBINARY(1024) NULL, ADD COLUMN `wechat_key_version` SMALLINT UNSIGNED NULL, ADD COLUMN `wechat_digest` CHAR(64) NULL',
  'SELECT 1') FROM information_schema.columns WHERE table_schema=@db AND table_name='job' AND column_name='phone_ciphertext');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- Equality digests are intentionally non-unique and have no full-text index.
SET @sql := (SELECT IF(COUNT(*) = 0,
  'ALTER TABLE `user` ADD KEY `idx_user_phone_digest` (`phone_digest`)',
  'SELECT 1') FROM information_schema.statistics WHERE table_schema=@db AND table_name='user' AND index_name='idx_user_phone_digest');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @sql := (SELECT IF(COUNT(*) = 0,
  'ALTER TABLE `job` ADD KEY `idx_job_phone_digest` (`phone_digest`)',
  'SELECT 1') FROM information_schema.statistics WHERE table_schema=@db AND table_name='job' AND index_name='idx_job_phone_digest');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
