-- Make Redis session transitions recoverable alongside the MySQL business
-- transaction. Safe to apply repeatedly on MySQL 8.0.

SET @schema_name = DATABASE();

SET @needs_status = (
    SELECT COUNT(*) = 0
      FROM information_schema.columns
     WHERE table_schema = @schema_name
       AND table_name = 'wecom_inbound_event'
       AND column_name = 'status'
       AND column_type LIKE '%session_pending%'
);
SET @ddl = IF(
    @needs_status,
    'ALTER TABLE `wecom_inbound_event`
       MODIFY COLUMN `status`
       ENUM(''received'',''processing'',''session_pending'',''done'',''failed'',''dead_letter'')
       NOT NULL DEFAULT ''received'' COMMENT ''处理状态''',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = @schema_name
          AND table_name = 'wecom_inbound_event'
          AND column_name = 'session_operation'
    ),
    'SELECT 1',
    'ALTER TABLE `wecom_inbound_event`
       ADD COLUMN `session_operation` VARCHAR(8) NULL AFTER `retry_count`,
       ADD COLUMN `session_expected_version` INT UNSIGNED NULL AFTER `session_operation`,
       ADD COLUMN `session_payload` JSON NULL AFTER `session_expected_version`,
       ADD COLUMN `session_apply_attempts` INT UNSIGNED NOT NULL DEFAULT 0 AFTER `session_payload`,
       ADD COLUMN `session_apply_locked_at` DATETIME(6) NULL AFTER `session_apply_attempts`,
       ADD COLUMN `session_next_attempt_at` DATETIME(6) NULL AFTER `session_apply_locked_at`,
       ADD COLUMN `session_applied_at` DATETIME(6) NULL AFTER `session_next_attempt_at`'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.statistics
        WHERE table_schema = @schema_name
          AND table_name = 'wecom_inbound_event'
          AND index_name = 'idx_session_commit_due'
    ),
    'SELECT 1',
    'ALTER TABLE `wecom_inbound_event`
       ADD INDEX `idx_session_commit_due`
       (`status`, `session_next_attempt_at`, `session_apply_locked_at`, `id`)'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
