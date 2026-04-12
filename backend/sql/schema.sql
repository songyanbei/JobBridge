-- ============================================================================
-- 招聘撮合平台 数据库 DDL
-- ============================================================================
-- 对应方案设计：方案设计_v0.1.md (v0.4)
-- DDL 版本：   v0.1
-- 生成日期：   2026-04-09
-- 目标数据库： MySQL 8.0+
-- 字符集：     utf8mb4 / utf8mb4_0900_ai_ci
-- 引擎：       InnoDB
-- ============================================================================
-- 表清单（共 11 张）：
--   user                   用户表（工人/厂家/中介）
--   job                    岗位信息表
--   resume                 简历信息表
--   conversation_log       对话历史日志（30 天）
--   audit_log              审核动作日志
--   dict_city              城市字典
--   dict_job_category      工种大类字典
--   dict_sensitive_word    敏感词字典
--   system_config          系统配置
--   admin_user             运营管理员账号
--   wecom_inbound_event    企微入站事件表（审计追溯 + 幂等 L2 防线）
-- ============================================================================
-- 设计说明：
-- 1. 会话状态（conversation_session）存 Redis，不在 MySQL，见方案 §14
-- 2. 所有业务表保留 `extra JSON` 扩展字段，避免前期频繁改表（见 §7.6）
-- 3. 所有需要 TTL 的表带 `expires_at`，由定时任务软/硬删除
-- 4. 简历的 `expected_cities` / `expected_job_categories` 用 JSON 数组存储，
--    当前规模（<2000 活跃简历）下直接 JSON_CONTAINS 过滤性能够用；
--    将来破万再考虑拆桥接表
-- ============================================================================

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- ============================================================================
-- 1. user 用户表
-- ============================================================================
DROP TABLE IF EXISTS `user`;
CREATE TABLE `user` (
    `external_userid`    VARCHAR(64)     NOT NULL                         COMMENT '企微外部联系人 ID，主键',
    `role`               ENUM('worker','factory','broker') NOT NULL       COMMENT '角色：工人/厂家/中介',
    `display_name`       VARCHAR(64)     DEFAULT NULL                     COMMENT '展示昵称',
    `company`            VARCHAR(128)    DEFAULT NULL                     COMMENT '公司名（厂家/中介填写）',
    `contact_person`     VARCHAR(64)     DEFAULT NULL                     COMMENT '联系人姓名',
    `phone`              VARCHAR(32)     DEFAULT NULL                     COMMENT '联系电话（工人侧不对外展示）',
    `can_search_jobs`    TINYINT(1)      NOT NULL DEFAULT 0               COMMENT '能否检索岗位（中介双向标记）',
    `can_search_workers` TINYINT(1)      NOT NULL DEFAULT 0               COMMENT '能否检索工人（中介双向标记）',
    `status`             ENUM('active','blocked','deleted') NOT NULL DEFAULT 'active' COMMENT '状态（deleted=用户行使被遗忘权后保留壳记录）',
    `blocked_reason`     VARCHAR(255)    DEFAULT NULL                     COMMENT '封禁原因',
    `registered_at`      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '注册时间',
    `last_active_at`     DATETIME        DEFAULT NULL                     COMMENT '最近活跃时间',
    `extra`              JSON            DEFAULT NULL                     COMMENT '扩展字段',
    PRIMARY KEY (`external_userid`),
    KEY `idx_role_status`  (`role`, `status`),
    KEY `idx_last_active`  (`last_active_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='用户表';


-- ============================================================================
-- 2. job 岗位信息表
-- ============================================================================
DROP TABLE IF EXISTS `job`;
CREATE TABLE `job` (
    `id`                       BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `owner_userid`             VARCHAR(64)     NOT NULL                     COMMENT '发布者 external_userid（厂家/中介）',

    -- ---- 硬过滤字段（§7.1）----
    `city`                     VARCHAR(32)     NOT NULL                     COMMENT '城市（强制归一到 dict_city）',
    `job_category`             VARCHAR(32)     NOT NULL                     COMMENT '工种大类（强制归一到 dict_job_category）',
    `salary_floor_monthly`     INT             NOT NULL                     COMMENT '月综合收入下限（元），见 §7.4 归一规则',
    `pay_type`                 ENUM('月薪','时薪','计件') NOT NULL            COMMENT '计薪方式',
    `headcount`                INT             NOT NULL                     COMMENT '还缺多少人，0 自动下架',
    `gender_required`          ENUM('男','女','不限') NOT NULL DEFAULT '不限' COMMENT '性别要求',
    `age_min`                  TINYINT UNSIGNED DEFAULT NULL                 COMMENT '年龄下限',
    `age_max`                  TINYINT UNSIGNED DEFAULT NULL                 COMMENT '年龄上限',
    `is_long_term`             TINYINT(1)      NOT NULL DEFAULT 1            COMMENT '1=长期工，0=短期工(<3个月)',

    -- ---- 软匹配字段（§7.1）----
    `district`                 VARCHAR(32)     DEFAULT NULL                  COMMENT '区县（细粒度）',
    `salary_ceiling_monthly`   INT             DEFAULT NULL                  COMMENT '月综合收入上限',
    `provide_meal`             TINYINT(1)      DEFAULT NULL                  COMMENT '包吃',
    `provide_housing`          TINYINT(1)      DEFAULT NULL                  COMMENT '包住',
    `dorm_condition`           VARCHAR(255)    DEFAULT NULL                  COMMENT '宿舍条件自由描述',
    `shift_pattern`            VARCHAR(128)    DEFAULT NULL                  COMMENT '班次模式（两班倒/白班/做六休一等）',
    `work_hours`               VARCHAR(128)    DEFAULT NULL                  COMMENT '工时描述',
    `accept_couple`            TINYINT(1)      DEFAULT NULL                  COMMENT '接受夫妻工',
    `accept_student`           TINYINT(1)      DEFAULT NULL                  COMMENT '接受学生工',
    `accept_minority`          TINYINT(1)      DEFAULT NULL                  COMMENT '接受少数民族',
    `height_required`          VARCHAR(32)     DEFAULT NULL                  COMMENT '身高要求',
    `experience_required`      VARCHAR(255)    DEFAULT NULL                  COMMENT '经验要求自由文本',
    `education_required`       ENUM('不限','初中','高中','中专','大专及以上') DEFAULT '不限',
    `rebate`                   VARCHAR(255)    DEFAULT NULL                  COMMENT '返费承诺',
    `employment_type`          ENUM('厂家直招','劳务派遣','中介代招') DEFAULT NULL,
    `contract_type`            ENUM('长期合同','短期合同','劳务关系')  DEFAULT NULL,
    `min_duration`             VARCHAR(64)     DEFAULT NULL                  COMMENT '最短做满多少天',
    `job_sub_category`         VARCHAR(64)     DEFAULT NULL                  COMMENT '工种子类（一期无字典，自由字符串）',

    -- ---- 原始描述 ----
    `raw_text`                 TEXT            NOT NULL                      COMMENT '用户原始提交',
    `description`              TEXT            DEFAULT NULL                  COMMENT 'IntentExtractor 清洗后的规范化描述',

    -- ---- 媒体 ----
    `images`                   JSON            DEFAULT NULL                  COMMENT '图片对象存储 key 数组（最多 5 张）',
    `miniprogram_url`          VARCHAR(512)    DEFAULT NULL                  COMMENT '小程序详情页链接',

    -- ---- 审核 ----
    `audit_status`             ENUM('pending','passed','rejected') NOT NULL DEFAULT 'pending',
    `audit_reason`             VARCHAR(255)    DEFAULT NULL                  COMMENT '审核理由（驳回时必填）',
    `audited_by`               VARCHAR(64)     DEFAULT NULL                  COMMENT '审核人（system / admin 用户名）',
    `audited_at`               DATETIME        DEFAULT NULL,

    -- ---- 生命周期 ----
    `created_at`               DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at`               DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    `expires_at`               DATETIME        NOT NULL                      COMMENT '过期时间（默认 created_at + 30 天）',
    `delist_reason`            ENUM('filled','manual_delist','expired') DEFAULT NULL COMMENT '下架原因：filled=已招满 / manual_delist=主动下架 / expired=TTL到期；null=在线',
    `deleted_at`               DATETIME        DEFAULT NULL                  COMMENT '软删除时间（null 代表有效）',

    -- ---- 乐观锁 ----
    `version`                  INT UNSIGNED    NOT NULL DEFAULT 1             COMMENT '乐观锁版本号，每次更新 +1（审核工作台用）',

    -- ---- 扩展 ----
    `extra`                    JSON            DEFAULT NULL                  COMMENT '扩展字段（§7.6）',

    PRIMARY KEY (`id`),
    KEY `idx_owner`       (`owner_userid`),
    KEY `idx_audit_time`  (`audit_status`, `created_at`),
    KEY `idx_expires`     (`expires_at`),
    -- 硬过滤复合索引：覆盖最热检索路径
    KEY `idx_filter_hot`  (`city`, `job_category`, `is_long_term`, `audit_status`, `deleted_at`, `expires_at`),
    KEY `idx_salary`      (`salary_floor_monthly`),
    CONSTRAINT `fk_job_owner` FOREIGN KEY (`owner_userid`) REFERENCES `user`(`external_userid`) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='岗位信息表';


-- ============================================================================
-- 3. resume 简历信息表
-- ============================================================================
DROP TABLE IF EXISTS `resume`;
CREATE TABLE `resume` (
    `id`                          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `owner_userid`                VARCHAR(64)     NOT NULL                  COMMENT '工人 external_userid',

    -- ---- 硬过滤字段（§7.2）----
    `expected_cities`             JSON            NOT NULL                  COMMENT '期望城市列表（至少一个），见 §7.2',
    `expected_job_categories`     JSON            NOT NULL                  COMMENT '期望工种大类列表',
    `salary_expect_floor_monthly` INT             NOT NULL                  COMMENT '期望月综合收入下限',
    `gender`                      ENUM('男','女') NOT NULL                   COMMENT '性别',
    `age`                         TINYINT UNSIGNED NOT NULL                 COMMENT '年龄',
    `accept_long_term`            TINYINT(1)      NOT NULL DEFAULT 1        COMMENT '接受长期工',
    `accept_short_term`           TINYINT(1)      NOT NULL DEFAULT 0        COMMENT '接受短期工',

    -- ---- 软匹配字段（§7.2）----
    `expected_districts`          JSON            DEFAULT NULL              COMMENT '期望区县',
    `height`                      SMALLINT UNSIGNED DEFAULT NULL            COMMENT '身高 cm',
    `weight`                      SMALLINT UNSIGNED DEFAULT NULL            COMMENT '体重 kg',
    `education`                   ENUM('不限','初中','高中','中专','大专及以上') DEFAULT '不限',
    `work_experience`             TEXT            DEFAULT NULL              COMMENT '工作经历自由文本',
    `accept_night_shift`          TINYINT(1)      DEFAULT NULL              COMMENT '接受倒班/夜班',
    `accept_standing_work`        TINYINT(1)      DEFAULT NULL              COMMENT '接受长时间站立',
    `accept_overtime`             TINYINT(1)      DEFAULT NULL              COMMENT '接受加班',
    `accept_outside_province`     TINYINT(1)      DEFAULT NULL              COMMENT '接受出省',
    `couple_seeking_together`     TINYINT(1)      DEFAULT NULL              COMMENT '夫妻同求',
    `has_health_certificate`      TINYINT(1)      DEFAULT NULL              COMMENT '持有健康证',
    `ethnicity`                   VARCHAR(32)     DEFAULT NULL              COMMENT '民族（匹配岗位 accept_minority）',
    `available_from`              DATE            DEFAULT NULL              COMMENT '可到岗日期',
    `has_tattoo`                  TINYINT(1)      DEFAULT NULL              COMMENT '有纹身',
    `taboo`                       VARCHAR(255)    DEFAULT NULL              COMMENT '禁忌自由文本（过敏/慢性病等）',

    -- ---- 原始描述 ----
    `raw_text`                    TEXT            NOT NULL                  COMMENT '用户原始提交',
    `description`                 TEXT            DEFAULT NULL              COMMENT 'IntentExtractor 清洗后的规范化描述',

    -- ---- 媒体 ----
    `images`                      JSON            DEFAULT NULL              COMMENT '图片对象存储 key 数组（最多 5 张）',
    `miniprogram_url`             VARCHAR(512)    DEFAULT NULL              COMMENT '小程序个人介绍链接',

    -- ---- 审核 ----
    `audit_status`                ENUM('pending','passed','rejected') NOT NULL DEFAULT 'pending',
    `audit_reason`                VARCHAR(255)    DEFAULT NULL,
    `audited_by`                  VARCHAR(64)     DEFAULT NULL,
    `audited_at`                  DATETIME        DEFAULT NULL,

    -- ---- 生命周期 ----
    `created_at`                  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at`                  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    `expires_at`                  DATETIME        NOT NULL                  COMMENT '过期时间（默认 created_at + 30 天）',
    `deleted_at`                  DATETIME        DEFAULT NULL,

    -- ---- 乐观锁 ----
    `version`                     INT UNSIGNED    NOT NULL DEFAULT 1        COMMENT '乐观锁版本号，每次更新 +1（审核工作台用）',

    -- ---- 扩展 ----
    `extra`                       JSON            DEFAULT NULL              COMMENT '扩展字段',

    PRIMARY KEY (`id`),
    KEY `idx_owner`        (`owner_userid`),
    KEY `idx_audit_time`   (`audit_status`, `created_at`),
    KEY `idx_expires`      (`expires_at`),
    KEY `idx_filter_hot`   (`gender`, `age`, `audit_status`, `deleted_at`, `expires_at`),
    KEY `idx_salary_exp`   (`salary_expect_floor_monthly`),
    CONSTRAINT `fk_resume_owner` FOREIGN KEY (`owner_userid`) REFERENCES `user`(`external_userid`) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='简历信息表';


-- ============================================================================
-- 4. conversation_log 对话历史日志（30 天 TTL）
-- ============================================================================
DROP TABLE IF EXISTS `conversation_log`;
CREATE TABLE `conversation_log` (
    `id`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `userid`            VARCHAR(64)     NOT NULL                     COMMENT 'external_userid',
    `direction`         ENUM('in','out') NOT NULL                    COMMENT 'in=用户发，out=系统回',
    `msg_type`          ENUM('text','image','voice','system') NOT NULL,
    `content`           MEDIUMTEXT      NOT NULL                     COMMENT '文本内容 or 媒体 key',
    `wecom_msg_id`      VARCHAR(64)     DEFAULT NULL                 COMMENT '企微消息 ID（幂等 L3 防线）',
    `intent`            VARCHAR(32)     DEFAULT NULL                 COMMENT '识别意图（search_job/search_worker/upload_job/upload_resume...）',
    `criteria_snapshot` JSON            DEFAULT NULL                 COMMENT '本轮 criteria 快照（调试与复现用）',
    `created_at`        DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `expires_at`        DATETIME        NOT NULL                     COMMENT '默认 created_at + 30 天',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_msg_id` (`wecom_msg_id`),
    KEY `idx_user_time` (`userid`, `created_at`),
    KEY `idx_expires`   (`expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='对话历史日志';


-- ============================================================================
-- 5. audit_log 审核动作日志
-- ============================================================================
DROP TABLE IF EXISTS `audit_log`;
CREATE TABLE `audit_log` (
    `id`           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `target_type`  ENUM('job','resume','user') NOT NULL            COMMENT '审核对象类型',
    `target_id`    VARCHAR(64)     NOT NULL                         COMMENT 'job.id / resume.id / user.external_userid',
    `action`       ENUM('auto_pass','auto_reject','manual_pass','manual_reject','appeal','reinstate') NOT NULL,
    `reason`       VARCHAR(255)    DEFAULT NULL                     COMMENT '动作原因',
    `operator`     VARCHAR(64)     DEFAULT NULL                     COMMENT 'system / admin 用户名',
    `snapshot`     JSON            DEFAULT NULL                     COMMENT '动作发生时的对象快照（可选）',
    `created_at`   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `idx_target` (`target_type`, `target_id`),
    KEY `idx_time`   (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='审核动作日志';


-- ============================================================================
-- 6. dict_city 城市字典
-- ============================================================================
DROP TABLE IF EXISTS `dict_city`;
CREATE TABLE `dict_city` (
    `id`          INT UNSIGNED NOT NULL AUTO_INCREMENT,
    `code`        VARCHAR(16)  NOT NULL                      COMMENT '国家统计局行政区划代码（6 位）',
    `name`        VARCHAR(32)  NOT NULL                      COMMENT '地级市规范名（例：苏州市）',
    `short_name`  VARCHAR(32)  DEFAULT NULL                  COMMENT '简称（例：苏州）',
    `province`    VARCHAR(32)  NOT NULL                      COMMENT '所属省份',
    `aliases`     JSON         DEFAULT NULL                  COMMENT '别名数组（例：["姑苏","苏州工业园区"]）',
    `enabled`     TINYINT(1)   NOT NULL DEFAULT 1,
    `updated_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_code` (`code`),
    KEY `idx_name` (`name`),
    KEY `idx_province` (`province`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='城市字典（全国地级市）';


-- ============================================================================
-- 7. dict_job_category 工种大类字典
-- ============================================================================
DROP TABLE IF EXISTS `dict_job_category`;
CREATE TABLE `dict_job_category` (
    `id`          INT UNSIGNED NOT NULL AUTO_INCREMENT,
    `code`        VARCHAR(32)  NOT NULL                      COMMENT '内部代码（例：electronic_factory）',
    `name`        VARCHAR(32)  NOT NULL                      COMMENT '显示名（例：电子厂）',
    `aliases`     JSON         DEFAULT NULL                  COMMENT '别名数组',
    `sort_order`  INT          NOT NULL DEFAULT 0            COMMENT '排序权重',
    `enabled`     TINYINT(1)   NOT NULL DEFAULT 1,
    `updated_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_code` (`code`),
    UNIQUE KEY `uk_name` (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='工种大类字典';


-- ============================================================================
-- 8. dict_sensitive_word 敏感词字典
-- ============================================================================
DROP TABLE IF EXISTS `dict_sensitive_word`;
CREATE TABLE `dict_sensitive_word` (
    `id`          INT UNSIGNED NOT NULL AUTO_INCREMENT,
    `word`        VARCHAR(64)  NOT NULL                      COMMENT '敏感词',
    `level`       ENUM('high','mid','low') NOT NULL DEFAULT 'mid' COMMENT 'high=直接拒 / mid=灰度 / low=仅打标',
    `category`    VARCHAR(32)  DEFAULT NULL                  COMMENT '分类（色情/政治/诈骗等）',
    `enabled`     TINYINT(1)   NOT NULL DEFAULT 1,
    `created_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_word` (`word`),
    KEY `idx_level_enabled` (`level`, `enabled`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='敏感词字典';


-- ============================================================================
-- 9. system_config 系统配置（KV 结构）
-- ============================================================================
DROP TABLE IF EXISTS `system_config`;
CREATE TABLE `system_config` (
    `config_key`   VARCHAR(64)  NOT NULL                     COMMENT '配置键',
    `config_value` TEXT         NOT NULL                     COMMENT '配置值（字符串 / JSON 字符串）',
    `value_type`   ENUM('string','int','bool','json') NOT NULL DEFAULT 'string',
    `description`  VARCHAR(255) DEFAULT NULL                 COMMENT '配置说明',
    `updated_at`   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    `updated_by`   VARCHAR(64)  DEFAULT NULL                 COMMENT '最近修改人',
    PRIMARY KEY (`config_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='系统配置';


-- ============================================================================
-- 10. admin_user 运营管理员账号
-- ============================================================================
DROP TABLE IF EXISTS `admin_user`;
CREATE TABLE `admin_user` (
    `id`             INT UNSIGNED NOT NULL AUTO_INCREMENT,
    `username`       VARCHAR(32)  NOT NULL                     COMMENT '登录用户名',
    `password_hash`  VARCHAR(128) NOT NULL                     COMMENT 'bcrypt 哈希',
    `display_name`   VARCHAR(64)  DEFAULT NULL                 COMMENT '显示名',
    `password_changed` TINYINT(1) NOT NULL DEFAULT 0             COMMENT '是否已修改初始密码（0=未改，首次登录强制改密码）',
    `enabled`        TINYINT(1)   NOT NULL DEFAULT 1,
    `last_login_at`  DATETIME     DEFAULT NULL,
    `created_at`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_username` (`username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='运营管理员账号';


-- ============================================================================
-- 11. wecom_inbound_event 企微入站事件表（§12.6.1）
-- ============================================================================
-- 用途：审计追溯 + 幂等 L2 防线 + Worker 处理状态监控
-- ============================================================================
DROP TABLE IF EXISTS `wecom_inbound_event`;
CREATE TABLE `wecom_inbound_event` (
    `id`                 BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `msg_id`             VARCHAR(64)     NOT NULL                     COMMENT '企微消息 ID，幂等键',
    `from_userid`        VARCHAR(64)     NOT NULL                     COMMENT '发送者 external_userid',
    `msg_type`           ENUM('text','image','voice','event') NOT NULL COMMENT '消息类型',
    `content_brief`      VARCHAR(500)    DEFAULT NULL                 COMMENT '消息摘要（文本取前 500 字，图片存 media_id）',
    `status`             ENUM('received','processing','done','failed','dead_letter') NOT NULL DEFAULT 'received' COMMENT '处理状态',
    `retry_count`        TINYINT UNSIGNED NOT NULL DEFAULT 0          COMMENT '已重试次数',
    `worker_started_at`  DATETIME        DEFAULT NULL                 COMMENT 'Worker 开始处理时间',
    `worker_finished_at` DATETIME        DEFAULT NULL                 COMMENT 'Worker 处理完成时间',
    `error_message`      TEXT            DEFAULT NULL                 COMMENT '失败原因',
    `created_at`         DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '回调到达时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_msg_id`    (`msg_id`),
    KEY `idx_status_time`     (`status`, `created_at`),
    KEY `idx_from_user`       (`from_userid`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='企微入站事件表';


SET FOREIGN_KEY_CHECKS = 1;

-- ============================================================================
-- 索引策略说明
-- ============================================================================
-- job.idx_filter_hot / resume.idx_filter_hot 是匹配引擎的核心索引，
-- 覆盖了硬过滤最常用的路径。顺序遵循"区分度高在前 + 常驻过滤在后"原则。
--
-- MySQL 8 对 JSON 列的 JSON_CONTAINS 查询虽然无索引，但 <2000 活跃记录下
-- 全表扫描也在 ms 级别。等到破万再考虑拆桥接表或用函数索引。
--
-- 所有 TTL 表都有 idx_expires，方便定时任务 WHERE expires_at < NOW() 扫描。
-- ============================================================================
