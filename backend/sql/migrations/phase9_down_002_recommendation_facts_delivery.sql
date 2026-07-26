-- Rollback for phase9_002_recommendation_facts_delivery.sql.
-- Execute after phase9_down_003 and before phase9_down_001.
-- MySQL 8.0 compatible and safe to apply repeatedly.
--
-- §11.1: only legal while the recommendation tables are still empty and the
-- feature was never enabled.  Drop order is foreign keys -> indexes -> columns
-- -> tables, and the circular request<->attempt reference is broken by dropping
-- `fk_recommendation_request_served_attempt` before the tables go away.
--
-- Restoring `wecom_outbound_outbox.content` to NOT NULL rewrites any NULL body
-- to an empty string first, otherwise the MODIFY aborts under strict mode.
-- Recommendation replies keep their body in the encrypted delivery envelope,
-- which is dropped by this same file, so there is nothing left to preserve.

SET @schema_name = DATABASE();

-- 1. wecom_outbound_outbox foreign key -------------------------------------
SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_schema = @schema_name
          AND table_name = 'wecom_outbound_outbox'
          AND constraint_name = 'fk_outbox_recommendation_delivery'
    ),
    'ALTER TABLE `wecom_outbound_outbox`
       DROP FOREIGN KEY `fk_outbox_recommendation_delivery`',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- 2. wecom_outbound_outbox unique key (backs the foreign key above) ---------
SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.statistics
        WHERE table_schema = @schema_name
          AND table_name = 'wecom_outbound_outbox'
          AND index_name = 'uk_outbox_recommendation_delivery'
    ),
    'ALTER TABLE `wecom_outbound_outbox`
       DROP INDEX `uk_outbox_recommendation_delivery`',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- 3. wecom_outbound_outbox.recommendation_delivery_id ----------------------
SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = @schema_name
          AND table_name = 'wecom_outbound_outbox'
          AND column_name = 'recommendation_delivery_id'
    ),
    'ALTER TABLE `wecom_outbound_outbox`
       DROP COLUMN `recommendation_delivery_id`',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- 4. wecom_outbound_outbox.content back to NOT NULL ------------------------
SET @outbox_content_nullable = (
    SELECT COUNT(*) > 0
      FROM information_schema.columns
     WHERE table_schema = @schema_name
       AND table_name = 'wecom_outbound_outbox'
       AND column_name = 'content'
       AND is_nullable = 'YES'
);

SET @ddl = IF(
    @outbox_content_nullable,
    'UPDATE `wecom_outbound_outbox` SET `content` = '''' WHERE `content` IS NULL',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(
    @outbox_content_nullable,
    'ALTER TABLE `wecom_outbound_outbox`
       MODIFY `content` MEDIUMTEXT NOT NULL',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- 5. conversation_log index -------------------------------------------------
SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.statistics
        WHERE table_schema = @schema_name
          AND table_name = 'conversation_log'
          AND index_name = 'idx_conversation_recommendation_delivery'
    ),
    'ALTER TABLE `conversation_log`
       DROP INDEX `idx_conversation_recommendation_delivery`',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- 6. conversation_log columns ----------------------------------------------
SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = @schema_name
          AND table_name = 'conversation_log'
          AND column_name = 'recommendation_delivery_id'
    ),
    'ALTER TABLE `conversation_log`
       DROP COLUMN `recommendation_delivery_id`,
       DROP COLUMN `redaction_state`',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- 7. break the request <-> attempt cycle ------------------------------------
SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_schema = @schema_name
          AND table_name = 'recommendation_request'
          AND constraint_name = 'fk_recommendation_request_served_attempt'
    ),
    'ALTER TABLE `recommendation_request`
       DROP FOREIGN KEY `fk_recommendation_request_served_attempt`',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- 8. fact tables, child first ----------------------------------------------
-- recommendation_delivery and recommendation_search_attempt both point at
-- recommendation_request, so the parent is dropped last.  Every other referrer
-- (event_log, wecom_outbound_outbox, recommendation_impression) was already
-- detached above or by phase9_down_003.
DROP TABLE IF EXISTS `recommendation_delivery`;
DROP TABLE IF EXISTS `recommendation_search_attempt`;
DROP TABLE IF EXISTS `recommendation_request`;
