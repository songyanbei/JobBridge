-- Rollback for phase9_006_event_client_idempotency.sql.
-- Execute after phase9_down_007 and before phase9_down_005.
-- MySQL 8.0 compatible and safe to apply repeatedly.
--
-- The unique key only covers event_log columns, no foreign key is backed by it,
-- so it can be dropped on its own.  event_log.client_event_id itself is removed
-- later by phase9_down_003 together with the rest of the attribution columns.

SET @schema_name = DATABASE();

SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.statistics
        WHERE table_schema = @schema_name
          AND table_name = 'event_log'
          AND index_name = 'uk_event_client_idempotency'
    ),
    'ALTER TABLE `event_log`
       DROP INDEX `uk_event_client_idempotency`',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
