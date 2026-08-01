-- Incremental upgrade after phase9_002 was released.
-- MySQL 8.0 compatible and safe to apply repeatedly.

SET @schema_name = DATABASE();

SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = @schema_name
          AND table_name = 'recommendation_delivery'
          AND column_name = 'content_expires_at'
    ),
    'SELECT 1',
    'ALTER TABLE `recommendation_delivery`
       ADD COLUMN `content_expires_at` DATETIME(6) NULL'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
