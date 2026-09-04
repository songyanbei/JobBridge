-- Phase 19: make recommendation_exposure_daily identity scope-aware.
--
-- The phase18 demo_id column is nullable by design so legacy rows remain
-- distinguishable from demo rows.  MySQL's old primary key omitted demo_id,
-- however, so a legacy and a demo aggregate for the same candidate/day could
-- not coexist.  Keep demo_id nullable and add a deterministic non-null scope
-- key ('' for legacy, demo_id for demo) to the primary key.
--
-- This migration is additive and retryable.  It never deletes or overwrites
-- aggregate data.  Before replacing the primary key it aborts if duplicate
-- scope identities already exist, so an operator can investigate safely.

SET @schema_name = DATABASE();

-- MySQL 8.0 has no ADD COLUMN IF NOT EXISTS; guard the addition explicitly.
SELECT COUNT(*) INTO @phase19_has_scope_key
FROM information_schema.columns
WHERE table_schema=@schema_name
  AND table_name='recommendation_exposure_daily'
  AND column_name='scope_key';
SET @ddl = CASE
    WHEN @phase19_has_scope_key > 0 THEN 'SELECT 1'
    ELSE CONCAT(
        'ALTER TABLE `recommendation_exposure_daily` ',
        'ADD COLUMN `scope_key` VARCHAR(64) NOT NULL DEFAULT ',
        CHAR(39), CHAR(39),
        ' COMMENT ', CHAR(39),
        'non-null scope key: empty for legacy, demo_id for demo rows',
        CHAR(39)
    )
END;
PREPARE phase19_stmt FROM @ddl;
EXECUTE phase19_stmt;
DEALLOCATE PREPARE phase19_stmt;

-- Backfill is deterministic and preserves the nullable demo_id contract.
UPDATE `recommendation_exposure_daily`
SET `scope_key` = COALESCE(`demo_id`, '');

-- Safety gate: never silently merge pre-existing aggregate rows.
SELECT COUNT(*) INTO @phase19_conflicts
FROM (
    SELECT `stat_date`, `target_type`, `target_id`, `scope_key`
    FROM `recommendation_exposure_daily`
    GROUP BY `stat_date`, `target_type`, `target_id`, `scope_key`
    HAVING COUNT(*) > 1
) AS phase19_duplicate_scopes;
SET @ddl = CASE
    WHEN @phase19_conflicts = 0 THEN 'SELECT 1'
    ELSE 'SELECT * FROM phase19_scope_conflict_abort'
END;
PREPARE phase19_stmt FROM @ddl;
EXECUTE phase19_stmt;
DEALLOCATE PREPARE phase19_stmt;

-- Prevent future writes from diverging scope_key and demo_id.
SELECT COUNT(*) INTO @phase19_has_scope_constraint
FROM information_schema.table_constraints
WHERE constraint_schema=@schema_name
  AND table_name='recommendation_exposure_daily'
  AND constraint_name='ck_recommendation_exposure_scope_key';
SET @ddl = CASE
    WHEN @phase19_has_scope_constraint > 0 THEN 'SELECT 1'
    ELSE CONCAT(
        'ALTER TABLE `recommendation_exposure_daily` ',
        'ADD CONSTRAINT `ck_recommendation_exposure_scope_key` ',
        'CHECK (`scope_key` = COALESCE(`demo_id`, ',
        CHAR(39), CHAR(39), '))'
    )
END;
PREPARE phase19_stmt FROM @ddl;
EXECUTE phase19_stmt;
DEALLOCATE PREPARE phase19_stmt;

-- Replace the legacy primary key only once.  The second guard also makes a
-- partially applied deployment safe to retry.
SELECT COUNT(*) INTO @phase19_has_primary
FROM information_schema.statistics
WHERE table_schema=@schema_name
  AND table_name='recommendation_exposure_daily'
  AND index_name='PRIMARY';
SELECT COUNT(*) INTO @phase19_has_scope_primary
FROM information_schema.statistics
WHERE table_schema=@schema_name
  AND table_name='recommendation_exposure_daily'
  AND index_name='PRIMARY'
  AND column_name='scope_key';
SET @ddl = CASE
    WHEN @phase19_has_scope_primary > 0 OR @phase19_has_primary = 0 THEN 'SELECT 1'
    ELSE 'ALTER TABLE `recommendation_exposure_daily` DROP PRIMARY KEY'
END;
PREPARE phase19_stmt FROM @ddl;
EXECUTE phase19_stmt;
DEALLOCATE PREPARE phase19_stmt;

SELECT COUNT(*) INTO @phase19_has_scope_primary
FROM information_schema.statistics
WHERE table_schema=@schema_name
  AND table_name='recommendation_exposure_daily'
  AND index_name='PRIMARY'
  AND column_name='scope_key';
SET @ddl = CASE
    WHEN @phase19_has_scope_primary > 0 THEN 'SELECT 1'
    ELSE 'ALTER TABLE `recommendation_exposure_daily`
       ADD PRIMARY KEY (`stat_date`, `target_type`, `target_id`, `scope_key`)'
END;
PREPARE phase19_stmt FROM @ddl;
EXECUTE phase19_stmt;
DEALLOCATE PREPARE phase19_stmt;
