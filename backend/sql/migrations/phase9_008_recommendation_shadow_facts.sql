-- Shadow comparison facts required by §7.5 / §9.4.
-- MySQL 8.0 compatible and safe to apply repeatedly.

SET @schema_name = DATABASE();

SET @ddl = IF(
    EXISTS(SELECT 1 FROM information_schema.columns
           WHERE table_schema = @schema_name AND table_name = 'recommendation_request'
             AND column_name = 'shadow_top_ids'),
    'SELECT 1',
    'ALTER TABLE `recommendation_request` ADD COLUMN `shadow_top_ids` JSON NULL'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(
    EXISTS(SELECT 1 FROM information_schema.columns
           WHERE table_schema = @schema_name AND table_name = 'recommendation_request'
             AND column_name = 'shadow_overlap_count'),
    'SELECT 1',
    'ALTER TABLE `recommendation_request` ADD COLUMN `shadow_overlap_count` INT UNSIGNED NULL'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(
    EXISTS(SELECT 1 FROM information_schema.columns
           WHERE table_schema = @schema_name AND table_name = 'recommendation_request'
             AND column_name = 'shadow_rank_delta'),
    'SELECT 1',
    'ALTER TABLE `recommendation_request` ADD COLUMN `shadow_rank_delta` JSON NULL'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(
    EXISTS(SELECT 1 FROM information_schema.columns
           WHERE table_schema = @schema_name AND table_name = 'recommendation_request'
             AND column_name = 'shadow_status'),
    'SELECT 1',
    'ALTER TABLE `recommendation_request` ADD COLUMN `shadow_status` VARCHAR(32) NULL'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(
    EXISTS(SELECT 1 FROM information_schema.columns
           WHERE table_schema = @schema_name AND table_name = 'recommendation_request'
             AND column_name = 'shadow_queue_wait_ms'),
    'SELECT 1',
    'ALTER TABLE `recommendation_request` ADD COLUMN `shadow_queue_wait_ms` INT UNSIGNED NULL'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(
    EXISTS(SELECT 1 FROM information_schema.columns
           WHERE table_schema = @schema_name AND table_name = 'recommendation_request'
             AND column_name = 'shadow_latency_ms'),
    'SELECT 1',
    'ALTER TABLE `recommendation_request` ADD COLUMN `shadow_latency_ms` INT UNSIGNED NULL'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(
    EXISTS(SELECT 1 FROM information_schema.columns
           WHERE table_schema = @schema_name AND table_name = 'recommendation_request'
             AND column_name = 'shadow_input_tokens'),
    'SELECT 1',
    'ALTER TABLE `recommendation_request` ADD COLUMN `shadow_input_tokens` INT UNSIGNED NULL'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(
    EXISTS(SELECT 1 FROM information_schema.columns
           WHERE table_schema = @schema_name AND table_name = 'recommendation_request'
             AND column_name = 'shadow_output_tokens'),
    'SELECT 1',
    'ALTER TABLE `recommendation_request` ADD COLUMN `shadow_output_tokens` INT UNSIGNED NULL'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(
    EXISTS(SELECT 1 FROM information_schema.columns
           WHERE table_schema = @schema_name AND table_name = 'recommendation_request'
             AND column_name = 'shadow_fallback'),
    'SELECT 1',
    'ALTER TABLE `recommendation_request` ADD COLUMN `shadow_fallback` VARCHAR(32) NULL'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
