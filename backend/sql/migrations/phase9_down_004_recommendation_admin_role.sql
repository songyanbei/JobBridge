-- Rollback for phase9_004_recommendation_admin_role.sql.
-- Execute after phase9_down_005 and before phase9_down_003.
-- MySQL 8.0 compatible and safe to apply repeatedly.
--
-- WARNING (§11.1): shrinking the audit_log enums is only legal while the
-- recommendation feature was never enabled, i.e. no audit_log row uses
-- target_type='recommendation_strategy' or any strategy_* action.  Under strict
-- mode MySQL aborts the MODIFY with errno 1265 if such rows exist.  This file
-- deliberately does NOT delete audit rows: forward-fix instead of destroying an
-- audit trail.

SET @schema_name = DATABASE();

-- 1. admin_user.role -------------------------------------------------------
SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = @schema_name
          AND table_name = 'admin_user'
          AND column_name = 'role'
    ),
    'ALTER TABLE `admin_user`
       DROP COLUMN `role`',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- 2. audit_log enums -------------------------------------------------------
SET @audit_extended = (
    SELECT COUNT(*) > 0
      FROM information_schema.columns
     WHERE table_schema = @schema_name
       AND table_name = 'audit_log'
       AND column_name = 'action'
       AND column_type LIKE '%strategy_kill_switch%'
);

SET @ddl = IF(
    @audit_extended,
    'ALTER TABLE `audit_log`
       MODIFY `action` ENUM(
         ''auto_pass'',''auto_reject'',''manual_pass'',''manual_reject'',''manual_edit'',
         ''undo'',''appeal'',''reinstate''
       ) NOT NULL,
       MODIFY `target_type` ENUM(
         ''job'',''resume'',''user'',''system''
       ) NOT NULL',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
