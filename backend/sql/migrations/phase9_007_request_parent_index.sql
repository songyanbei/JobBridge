-- Lineage lookup for auto-relaxed / show_more child requests.
-- MySQL 8.0 compatible and safe to apply repeatedly.
--
-- phase9_002 already declares fk_recommendation_request_parent, and MySQL backs
-- every foreign key with an index.  Guard on the *column* rather than the index
-- name so this never creates a redundant duplicate index.

SET @schema_name = DATABASE();

SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.statistics
        WHERE table_schema = @schema_name
          AND table_name = 'recommendation_request'
          AND column_name = 'parent_request_id'
          AND seq_in_index = 1
    ),
    'SELECT 1',
    'ALTER TABLE `recommendation_request`
       ADD KEY `idx_recommendation_request_parent` (`parent_request_id`)'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
