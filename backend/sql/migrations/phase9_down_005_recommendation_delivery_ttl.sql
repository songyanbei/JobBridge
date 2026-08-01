-- Rollback for phase9_005_recommendation_delivery_ttl.sql.
-- Execute after phase9_down_006 and before phase9_down_004.
-- MySQL 8.0 compatible and safe to apply repeatedly.
--
-- The column carries no index and no foreign key, so the guard on
-- information_schema.columns is enough.  When recommendation_delivery no longer
-- exists (phase9_down_002 already ran) the guard simply evaluates to false.

SET @schema_name = DATABASE();

SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = @schema_name
          AND table_name = 'recommendation_delivery'
          AND column_name = 'content_expires_at'
    ),
    'ALTER TABLE `recommendation_delivery`
       DROP COLUMN `content_expires_at`',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
