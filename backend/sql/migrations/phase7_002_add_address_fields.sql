-- ============================================================================
-- Phase 7 migration #002: 给 user / job 表加 address 字段
--
-- Context:
--   客户明确提出三层数据隔离需求：工人不可见厂家电话/地址，中介与厂家可见。
--   电话已有（user.phone，岗位侧复用 owner.phone，不在 job 表冗余）。
--   地址此前缺失：
--     - user 表只有 company（公司名），无公司地址；
--     - job 表只有 city（城市）和 district（区县），无街道门牌等详细地址。
--
--   本迁移补这两个字段，前端展示与 API 隔离过滤都依赖它们。
--
-- Schema 改动：
--   user.address  VARCHAR(255) NULL   COMMENT '公司/经营地址（厂家/中介填写）'
--   job.address   VARCHAR(255) NULL   COMMENT '岗位详细工作地址（街道+门牌）'
--
-- 同步真值：backend/sql/schema.sql 已加这两列；本迁移面向已上线的旧库。
--
-- 隔离逻辑（后续实现，不在本迁移范围）：
--   API 返回岗位详情时，按调用者 role 决定是否携带 owner.phone / owner.address
--   / job.address。一期建议在 admin 路由 schema 里做白名单。
--
-- 幂等：用 INFORMATION_SCHEMA 探测后再 ALTER，反复执行安全。
-- ============================================================================

-- user.address
SET @col_exists := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME   = 'user'
      AND COLUMN_NAME  = 'address'
);
SET @ddl := IF(@col_exists = 0,
    "ALTER TABLE `user` ADD COLUMN `address` VARCHAR(255) DEFAULT NULL COMMENT '公司/经营地址（厂家/中介填写）' AFTER `company`",
    "SELECT 'user.address already exists, skip' AS info"
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- job.address
SET @col_exists := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME   = 'job'
      AND COLUMN_NAME  = 'address'
);
SET @ddl := IF(@col_exists = 0,
    "ALTER TABLE `job` ADD COLUMN `address` VARCHAR(255) DEFAULT NULL COMMENT '岗位详细工作地址（街道+门牌，区县另见 district）' AFTER `district`",
    "SELECT 'job.address already exists, skip' AS info"
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- 验证：期望 user/job 各自看到 address 字段
SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, COLUMN_COMMENT
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
  AND COLUMN_NAME  = 'address'
  AND TABLE_NAME IN ('user', 'job');
