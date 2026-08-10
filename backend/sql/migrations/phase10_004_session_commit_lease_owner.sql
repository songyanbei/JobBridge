-- Fence durable session commit checkpoints with a unique owner per claim.
-- Stop all old workers before applying this migration because mixed workers do
-- not populate the owner token. Safe to apply repeatedly on MySQL 8.0.

SET @schema_name = DATABASE();

SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = @schema_name
          AND table_name = 'wecom_inbound_event'
          AND column_name = 'session_apply_lease_owner'
    ),
    'SELECT 1',
    'ALTER TABLE `wecom_inbound_event`
       ADD COLUMN `session_apply_lease_owner` VARCHAR(64) NULL
       AFTER `session_apply_locked_at`'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
