-- Phase 9: admin RBAC column and the recommendation audit enum extensions.
-- MySQL 8.0 compatible and safe to apply repeatedly.
--
-- §9.10 only migrates *existing* accounts to super_admin.  The column default is
-- therefore the least-privileged role, so accounts created after this migration
-- must pick a role explicitly instead of silently inheriting full control.

SET @schema_name = DATABASE();

SET @role_missing = (
    SELECT COUNT(*) = 0
      FROM information_schema.columns
     WHERE table_schema = @schema_name
       AND table_name = 'admin_user'
       AND column_name = 'role'
);

SET @ddl = IF(
    @role_missing,
    'ALTER TABLE `admin_user`
       ADD COLUMN `role` ENUM(''viewer'',''operator'',''super_admin'')
       NOT NULL DEFAULT ''viewer'' COMMENT ''管理员角色'' AFTER `display_name`',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- Only the accounts that predate the column are grandfathered in.
SET @ddl = IF(
    @role_missing,
    'UPDATE `admin_user` SET `role` = ''super_admin''',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @audit_stale = (
    SELECT COUNT(*) = 0
      FROM information_schema.columns
     WHERE table_schema = @schema_name
       AND table_name = 'audit_log'
       AND column_name = 'action'
       AND column_type LIKE '%strategy_kill_switch%'
);

SET @ddl = IF(
    @audit_stale,
    'ALTER TABLE `audit_log`
       MODIFY `target_type` ENUM(
         ''job'',''resume'',''user'',''system'',''recommendation_strategy''
       ) NOT NULL,
       MODIFY `action` ENUM(
         ''auto_pass'',''auto_reject'',''manual_pass'',''manual_reject'',''manual_edit'',
         ''undo'',''appeal'',''reinstate'',''strategy_publish'',''strategy_rollout'',
         ''strategy_promote'',''strategy_rollback'',''strategy_kill_switch''
       ) NOT NULL',
    'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
