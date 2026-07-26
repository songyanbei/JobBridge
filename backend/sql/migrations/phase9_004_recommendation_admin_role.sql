ALTER TABLE admin_user
  ADD COLUMN IF NOT EXISTS role ENUM('viewer','operator','super_admin') NOT NULL DEFAULT 'super_admin';
UPDATE admin_user SET role = 'super_admin' WHERE role IS NULL OR role = '';

ALTER TABLE audit_log
  MODIFY target_type ENUM(
    'job','resume','user','system','recommendation_strategy'
  ) NOT NULL,
  MODIFY action ENUM(
    'auto_pass','auto_reject','manual_pass','manual_reject','manual_edit',
    'undo','appeal','reinstate','strategy_publish','strategy_rollout',
    'strategy_promote','strategy_rollback','strategy_kill_switch'
  ) NOT NULL;
