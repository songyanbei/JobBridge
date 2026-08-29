-- Phase 0 durable inbox compatibility extension.
-- Additive and safe to run repeatedly on MySQL 8.0; never changes the
-- existing wecom_event_status enum or introduces accepted/rate_limited status.

SET @schema_name = DATABASE();

SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = @schema_name
          AND table_name = 'wecom_inbound_event'
          AND column_name = 'turn_id'
    ),
    'SELECT 1',
    'ALTER TABLE `wecom_inbound_event`
       ADD COLUMN `turn_id` CHAR(36) NULL
       AFTER `msg_id`'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- Existing rows predate turn IDs. UUID() is generated once per row and is not
-- reused by retries; the final NOT NULL/unique constraints are applied below.
UPDATE `wecom_inbound_event`
   SET `turn_id` = UUID()
 WHERE `turn_id` IS NULL;

SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.statistics
        WHERE table_schema = @schema_name
          AND table_name = 'wecom_inbound_event'
          AND index_name = 'uk_turn_id'
    ),
    'SELECT 1',
    'ALTER TABLE `wecom_inbound_event`
       ADD UNIQUE KEY `uk_turn_id` (`turn_id`)'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

ALTER TABLE `wecom_inbound_event`
  MODIFY COLUMN `turn_id` CHAR(36) NOT NULL
  COMMENT '不可变入站轮次 ID；重试复用，人工重放新建';

SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = @schema_name
          AND table_name = 'wecom_inbound_event'
          AND column_name = 'rate_limit_decision'
    ),
    'SELECT 1',
    'ALTER TABLE `wecom_inbound_event`
       ADD COLUMN `rate_limit_decision` ENUM(''accepted'',''rate_limited'')
       NULL DEFAULT ''accepted''
       AFTER `status`'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

UPDATE `wecom_inbound_event`
   SET `rate_limit_decision` = 'accepted'
 WHERE `rate_limit_decision` IS NULL;

ALTER TABLE `wecom_inbound_event`
  MODIFY COLUMN `rate_limit_decision`
    ENUM('accepted','rate_limited') NOT NULL DEFAULT 'accepted'
    COMMENT '限流审计决策';

SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = @schema_name
          AND table_name = 'wecom_inbound_event'
          AND column_name = 'rate_limit_rule'
    ),
    'SELECT 1',
    'ALTER TABLE `wecom_inbound_event`
       ADD COLUMN `rate_limit_rule` VARCHAR(128) NULL
       COMMENT ''限流规则/版本（仅审计）''
       AFTER `rate_limit_decision`'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = @schema_name
          AND table_name = 'wecom_inbound_event'
          AND column_name = 'rate_limited_at'
    ),
    'SELECT 1',
    'ALTER TABLE `wecom_inbound_event`
       ADD COLUMN `rate_limited_at` DATETIME(6) NULL
       COMMENT ''限流决策时间''
       AFTER `rate_limit_rule`'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.statistics
        WHERE table_schema = @schema_name
          AND table_name = 'wecom_inbound_event'
          AND index_name = 'idx_inbound_dispatch'
    ),
    'SELECT 1',
    'ALTER TABLE `wecom_inbound_event`
       ADD KEY `idx_inbound_dispatch`
       (`status`,`rate_limit_decision`,`created_at`,`id`)'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
