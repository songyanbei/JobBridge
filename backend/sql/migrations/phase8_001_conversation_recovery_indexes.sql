-- Conversation worker recovery/order indexes.
--
-- Safe to run repeatedly on MySQL 8.0. Existing installations created from
-- schema.sql only have idx_status_time and idx_from_user; periodic recovery
-- otherwise scans large status partitions every 30 seconds.

SET @schema_name = DATABASE();

SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.statistics
        WHERE table_schema = @schema_name
          AND table_name = 'wecom_inbound_event'
          AND index_name = 'idx_status_worker_started'
    ),
    'SELECT 1',
    'ALTER TABLE `wecom_inbound_event` ADD INDEX `idx_status_worker_started` (`status`, `worker_started_at`)'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.statistics
        WHERE table_schema = @schema_name
          AND table_name = 'wecom_inbound_event'
          AND index_name = 'idx_status_worker_finished'
    ),
    'SELECT 1',
    'ALTER TABLE `wecom_inbound_event` ADD INDEX `idx_status_worker_finished` (`status`, `worker_finished_at`)'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.statistics
        WHERE table_schema = @schema_name
          AND table_name = 'wecom_inbound_event'
          AND index_name = 'idx_user_status_id'
    ),
    'SELECT 1',
    'ALTER TABLE `wecom_inbound_event` ADD INDEX `idx_user_status_id` (`from_userid`, `status`, `id`)'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
