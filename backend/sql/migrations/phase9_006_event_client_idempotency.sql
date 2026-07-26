-- Client-supplied click idempotency key.
-- MySQL 8.0 compatible and safe to apply repeatedly.

SET @schema_name = DATABASE();

SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.statistics
        WHERE table_schema = @schema_name
          AND table_name = 'event_log'
          AND index_name = 'uk_event_client_idempotency'
    ),
    'SELECT 1',
    'ALTER TABLE `event_log`
       ADD UNIQUE KEY `uk_event_client_idempotency` (`userid`, `event_type`, `client_event_id`)'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
