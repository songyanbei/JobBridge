-- Phase 10: 岗位级招聘主体、地址和联系方式
-- 幂等迁移；不会覆盖已有数据。

SET @col_exists := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'job'
      AND COLUMN_NAME = 'hiring_company'
);
SET @ddl := IF(@col_exists = 0,
    "ALTER TABLE `job` ADD COLUMN `hiring_company` VARCHAR(128) DEFAULT NULL COMMENT '实际招聘工厂名（岗位级）' AFTER `owner_userid`",
    "SELECT 'job.hiring_company already exists, skip' AS info"
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @col_exists := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'job'
      AND COLUMN_NAME = 'contact_person'
);
SET @ddl := IF(@col_exists = 0,
    "ALTER TABLE `job` ADD COLUMN `contact_person` VARCHAR(64) DEFAULT NULL COMMENT '岗位级联系人（覆盖发布账号）' AFTER `address`",
    "SELECT 'job.contact_person already exists, skip' AS info"
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @col_exists := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'job'
      AND COLUMN_NAME = 'phone'
);
SET @ddl := IF(@col_exists = 0,
    "ALTER TABLE `job` ADD COLUMN `phone` VARCHAR(32) DEFAULT NULL COMMENT '岗位级联系电话（覆盖发布账号）' AFTER `contact_person`",
    "SELECT 'job.phone already exists, skip' AS info"
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, COLUMN_COMMENT
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME = 'job'
  AND COLUMN_NAME IN ('hiring_company', 'address', 'contact_person', 'phone');
