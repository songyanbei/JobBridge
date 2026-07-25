-- Preserve efficient per-user reply ordering for installations that already
-- applied phase8_003 before the ordering index was added.

SET @schema_name = DATABASE();
SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.statistics
        WHERE table_schema = @schema_name
          AND table_name = 'wecom_outbound_outbox'
          AND index_name = 'idx_outbox_user_status_id'
    ),
    'SELECT 1',
    'ALTER TABLE `wecom_outbound_outbox`
       ADD INDEX `idx_outbox_user_status_id` (`userid`, `status`, `id`)'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
