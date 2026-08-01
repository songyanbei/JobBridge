-- Rollback for phase9_007_request_parent_index.sql (§11.1 "提供回滚 SQL").
--
-- Down migrations must be executed in reverse order: 007 -> 006 -> ... -> 001.
-- They are NOT listed in phase9_manifest.sha256 on purpose: the apply script
-- executes every manifest entry in order, so a down file in the manifest would
-- immediately undo the migration it follows.
--
-- MySQL 8.0 has no `DROP INDEX IF EXISTS` / `DROP COLUMN IF EXISTS`, so every
-- drop is guarded through information_schema with the same
-- SET @ddl = IF(...) / PREPARE / EXECUTE / DEALLOCATE pattern as the up files.
--
-- phase9_002 already declares fk_recommendation_request_parent and InnoDB backs
-- it with an auto-created index named after the constraint, which makes
-- phase9_007 a no-op on a fully migrated database.  Only drop the standalone
-- index when it is *not* the index that currently backs the foreign key,
-- otherwise MySQL rejects the drop with errno 1553.

SET @schema_name = DATABASE();

SET @can_drop_parent_index = (
    SELECT EXISTS(
        SELECT 1 FROM information_schema.statistics
        WHERE table_schema = @schema_name
          AND table_name = 'recommendation_request'
          AND index_name = 'idx_recommendation_request_parent'
    )
    AND (
        NOT EXISTS(
            SELECT 1 FROM information_schema.table_constraints
            WHERE table_schema = @schema_name
              AND table_name = 'recommendation_request'
              AND constraint_name = 'fk_recommendation_request_parent'
        )
        OR EXISTS(
            SELECT 1 FROM information_schema.statistics
            WHERE table_schema = @schema_name
              AND table_name = 'recommendation_request'
              AND column_name = 'parent_request_id'
              AND seq_in_index = 1
              AND index_name <> 'idx_recommendation_request_parent'
        )
    )
);

SET @ddl = IF(
    @can_drop_parent_index,
    'ALTER TABLE `recommendation_request`
       DROP INDEX `idx_recommendation_request_parent`',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
