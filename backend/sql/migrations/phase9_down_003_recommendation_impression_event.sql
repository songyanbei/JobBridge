-- Rollback for phase9_003_recommendation_impression_event.sql.
-- Execute after phase9_down_004 and before phase9_down_002.
-- MySQL 8.0 compatible and safe to apply repeatedly.
--
-- Drop order matters: the foreign key goes first, then the indexes, then the
-- columns, then the tables.  `idx_event_delivery_target` leads with
-- `delivery_id`, so InnoDB reuses it as the backing index of
-- `fk_event_recommendation_delivery`, so dropping the index while the
-- constraint still exists fails with errno 1553.

SET @schema_name = DATABASE();

-- 1. event_log foreign key -------------------------------------------------
SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_schema = @schema_name
          AND table_name = 'event_log'
          AND constraint_name = 'fk_event_recommendation_delivery'
    ),
    'ALTER TABLE `event_log`
       DROP FOREIGN KEY `fk_event_recommendation_delivery`',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- 2. event_log attribution indexes -----------------------------------------
SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.statistics
        WHERE table_schema = @schema_name
          AND table_name = 'event_log'
          AND index_name = 'idx_event_delivery_target'
    ),
    'ALTER TABLE `event_log`
       DROP INDEX `idx_event_delivery_target`',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.statistics
        WHERE table_schema = @schema_name
          AND table_name = 'event_log'
          AND index_name = 'idx_event_attributed_version'
    ),
    'ALTER TABLE `event_log`
       DROP INDEX `idx_event_attributed_version`',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.statistics
        WHERE table_schema = @schema_name
          AND table_name = 'event_log'
          AND index_name = 'idx_event_attribution_status'
    ),
    'ALTER TABLE `event_log`
       DROP INDEX `idx_event_attribution_status`',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.statistics
        WHERE table_schema = @schema_name
          AND table_name = 'event_log'
          AND index_name = 'uk_event_attribution_dedupe'
    ),
    'ALTER TABLE `event_log`
       DROP INDEX `uk_event_attribution_dedupe`',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- 3. event_log attribution columns -----------------------------------------
-- Defensive: phase9_down_006 normally removed `uk_event_client_idempotency`
-- already.  If this file is run out of order the index would still be there,
-- and dropping `client_event_id` would leave MySQL rebuilding it as a UNIQUE
-- key over (`userid`, `event_type`) alone, which blows up on duplicates.
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

-- phase9_003 added the ten columns in a single guarded ALTER keyed on
-- `delivery_id`, so the inverse uses the same key.
SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = @schema_name
          AND table_name = 'event_log'
          AND column_name = 'delivery_id'
    ),
    'ALTER TABLE `event_log`
       DROP COLUMN `delivery_id`,
       DROP COLUMN `request_id`,
       DROP COLUMN `snapshot_id`,
       DROP COLUMN `position`,
       DROP COLUMN `attribution_status`,
       DROP COLUMN `attributed_strategy_version_id`,
       DROP COLUMN `attributed_algorithm_version`,
       DROP COLUMN `attributed_is_exploration`,
       DROP COLUMN `client_event_id`,
       DROP COLUMN `attribution_dedupe_key`',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- 4. exposure tables -------------------------------------------------------
-- Nothing references these two, and `DROP TABLE IF EXISTS` is natively
-- idempotent, so no information_schema guard is required here.
DROP TABLE IF EXISTS `recommendation_exposure_daily`;
DROP TABLE IF EXISTS `recommendation_impression`;
