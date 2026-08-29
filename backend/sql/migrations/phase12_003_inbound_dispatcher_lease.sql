-- Durable lease for the Phase 0 accepted-inbox dispatcher.
-- Additive and safe to run repeatedly on MySQL 8.0.

SET @schema_name = DATABASE();

SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = @schema_name
          AND table_name = 'wecom_inbound_event'
          AND column_name = 'dispatcher_lease_owner'
    ),
    'SELECT 1',
    'ALTER TABLE `wecom_inbound_event`
       ADD COLUMN `dispatcher_lease_owner` VARCHAR(64) NULL
       COMMENT ''入站 dispatcher 当前 owner''
       AFTER `rate_limited_at`'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = @schema_name
          AND table_name = 'wecom_inbound_event'
          AND column_name = 'dispatcher_lease_expires_at'
    ),
    'SELECT 1',
    'ALTER TABLE `wecom_inbound_event`
       ADD COLUMN `dispatcher_lease_expires_at` DATETIME(6) NULL
       COMMENT ''入站 dispatcher lease 到期时间''
       AFTER `dispatcher_lease_owner`'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.statistics
        WHERE table_schema = @schema_name
          AND table_name = 'wecom_inbound_event'
          AND index_name = 'idx_inbound_dispatch_lease'
    ),
    'SELECT 1',
    'ALTER TABLE `wecom_inbound_event`
       ADD KEY `idx_inbound_dispatch_lease`
       (`status`,`rate_limit_decision`,`dispatcher_lease_expires_at`,`id`)'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
