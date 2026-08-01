-- Rollback for phase9_001_recommendation_strategy.sql.
-- Execute last, after phase9_down_002.
-- MySQL 8.0 compatible and safe to apply repeatedly.
--
-- Removes the control-plane seed rows that phase9_001 pushed in with
-- INSERT IGNORE before dropping the four tables, so an operator who only wants
-- to undo the seeding can stop after the DELETE section.  The DELETEs are
-- guarded on table existence so the file stays re-runnable once the tables are
-- gone.  No other table holds a foreign key onto these four.

SET @schema_name = DATABASE();

-- 1. seed rows from phase9_001 ---------------------------------------------
SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = @schema_name
          AND table_name = 'recommendation_release_history'
    ),
    'DELETE FROM `recommendation_release_history`
      WHERE `direction` IN (''search_job'',''search_worker'')
        AND `revision` = 1
        AND `operation` = ''init''',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = @schema_name
          AND table_name = 'recommendation_runtime_control'
    ),
    'DELETE FROM `recommendation_runtime_control` WHERE `scope` = ''global''',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = @schema_name
          AND table_name = 'recommendation_strategy_release'
    ),
    'DELETE FROM `recommendation_strategy_release`
      WHERE `direction` IN (''search_job'',''search_worker'')',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- 2. control-plane tables ---------------------------------------------------
DROP TABLE IF EXISTS `recommendation_runtime_control`;
DROP TABLE IF EXISTS `recommendation_release_history`;
DROP TABLE IF EXISTS `recommendation_strategy_release`;
DROP TABLE IF EXISTS `recommendation_strategy_version`;
