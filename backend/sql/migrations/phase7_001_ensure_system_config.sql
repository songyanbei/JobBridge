-- ============================================================================
-- Phase 7 migration #001: ensure TTL-related system_config keys exist.
--
-- Context:
--   seed.sql 只在 Docker MySQL 首次初始化时由 /docker-entrypoint-initdb.d 执行；
--   已上线环境 (已有 mysql_data volume) 不会重新执行 seed。Phase 7 新增的
--     - ttl.audit_log.days           (default 180)
--     - ttl.wecom_inbound_event.days (default 30)
--     - ttl.hard_delete.delay_days   (default 7)
--   在旧库中缺失时，后端会回落到代码 hardcode 默认值，导致运营在后台"系统配置"
--   修改这几个 key 时"感觉改了但不生效"。
--
-- Fix path:
--   1) 本脚本：所有既有环境升级到 Phase 7 时必须执行一次（幂等，反复执行安全）
--   2) 应用层自愈：app 启动与每次 ttl_cleanup.run() 会调用
--      app.tasks.common.ensure_ttl_config_defaults()，缺失时补齐并写 warning 日志
--
-- Execution (既有环境升级)：
--   docker exec -i jobbridge-mysql mysql -u root -p"$MYSQL_ROOT_PASSWORD" jobbridge \
--     < backend/sql/migrations/phase7_001_ensure_system_config.sql
--
-- Idempotent: INSERT IGNORE 遇到 config_key 主键冲突时跳过，不覆盖已有配置。
-- ============================================================================

INSERT IGNORE INTO `system_config`
  (`config_key`, `config_value`, `value_type`, `description`)
VALUES
  ('ttl.job.days',                 '30',  'int', '岗位 TTL（天）'),
  ('ttl.resume.days',              '30',  'int', '简历 TTL（天）'),
  ('ttl.conversation_log.days',    '30',  'int', '对话日志 TTL（天）'),
  ('ttl.audit_log.days',           '180', 'int', '审核日志 TTL（天）— Phase 7 新增'),
  ('ttl.wecom_inbound_event.days', '30',  'int', '入站事件表 TTL（天）— Phase 7 新增'),
  ('ttl.hard_delete.delay_days',   '7',   'int', '软删到硬删延迟（天）— Phase 7 新增');

-- 验证：期望输出 6 行 ttl.* 配置
SELECT config_key, config_value, value_type
FROM system_config
WHERE config_key LIKE 'ttl.%'
ORDER BY config_key;
