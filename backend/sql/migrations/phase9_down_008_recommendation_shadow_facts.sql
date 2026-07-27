-- Rollback for phase9_008_recommendation_shadow_facts.sql.
-- MySQL 8.0 compatible and safe to apply repeatedly.

SET @schema_name = DATABASE();

SET @ddl = IF(
    EXISTS(SELECT 1 FROM information_schema.columns
           WHERE table_schema = @schema_name AND table_name = 'recommendation_request'
             AND column_name = 'shadow_fallback'),
    'ALTER TABLE `recommendation_request` DROP COLUMN `shadow_fallback`',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(
    EXISTS(SELECT 1 FROM information_schema.columns
           WHERE table_schema = @schema_name AND table_name = 'recommendation_request'
             AND column_name = 'shadow_output_tokens'),
    'ALTER TABLE `recommendation_request` DROP COLUMN `shadow_output_tokens`',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(
    EXISTS(SELECT 1 FROM information_schema.columns
           WHERE table_schema = @schema_name AND table_name = 'recommendation_request'
             AND column_name = 'shadow_input_tokens'),
    'ALTER TABLE `recommendation_request` DROP COLUMN `shadow_input_tokens`',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(
    EXISTS(SELECT 1 FROM information_schema.columns
           WHERE table_schema = @schema_name AND table_name = 'recommendation_request'
             AND column_name = 'shadow_latency_ms'),
    'ALTER TABLE `recommendation_request` DROP COLUMN `shadow_latency_ms`',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(
    EXISTS(SELECT 1 FROM information_schema.columns
           WHERE table_schema = @schema_name AND table_name = 'recommendation_request'
             AND column_name = 'shadow_queue_wait_ms'),
    'ALTER TABLE `recommendation_request` DROP COLUMN `shadow_queue_wait_ms`',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(
    EXISTS(SELECT 1 FROM information_schema.columns
           WHERE table_schema = @schema_name AND table_name = 'recommendation_request'
             AND column_name = 'shadow_status'),
    'ALTER TABLE `recommendation_request` DROP COLUMN `shadow_status`',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(
    EXISTS(SELECT 1 FROM information_schema.columns
           WHERE table_schema = @schema_name AND table_name = 'recommendation_request'
             AND column_name = 'shadow_rank_delta'),
    'ALTER TABLE `recommendation_request` DROP COLUMN `shadow_rank_delta`',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(
    EXISTS(SELECT 1 FROM information_schema.columns
           WHERE table_schema = @schema_name AND table_name = 'recommendation_request'
             AND column_name = 'shadow_overlap_count'),
    'ALTER TABLE `recommendation_request` DROP COLUMN `shadow_overlap_count`',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(
    EXISTS(SELECT 1 FROM information_schema.columns
           WHERE table_schema = @schema_name AND table_name = 'recommendation_request'
             AND column_name = 'shadow_top_ids'),
    'ALTER TABLE `recommendation_request` DROP COLUMN `shadow_top_ids`',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
