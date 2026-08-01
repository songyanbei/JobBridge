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
-- 表清单（共 22 张）：
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
--   wecom_outbound_outbox  企微回复事务出站箱
--   event_log              外部事件回传日志
--   -- 推荐策略与曝光多样性 v1（phase9_001..007，方案 §9）--
--   recommendation_strategy_version   策略版本（草稿/发布/归档）
--   recommendation_strategy_release   方向级发布状态（灰度/主备版本）
--   recommendation_release_history    发布操作台账
--   recommendation_runtime_control    运行时总闸（kill switch）
--   recommendation_request            推荐请求事实表
--   recommendation_search_attempt     单次检索尝试事实表
--   recommendation_delivery           推荐投递出站箱（密文正文 + 曝光派生）
--   recommendation_impression         曝光事实表
--   recommendation_exposure_daily     曝光日聚合
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
    `address`            VARCHAR(255)    DEFAULT NULL                     COMMENT '公司/经营地址（厂家/中介填写）',
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
    `address`                  VARCHAR(255)    DEFAULT NULL                  COMMENT '岗位详细工作地址（街道+门牌，区县另见 district）',
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
    `recommendation_delivery_id` CHAR(36) DEFAULT NULL               COMMENT '推荐投递 ID（仅 v1 推荐日志赋值，旧非推荐日志为 NULL）',
    `redaction_state`   VARCHAR(24)     DEFAULT NULL                 COMMENT '脱敏状态（推荐日志一律占位符，不回填历史明文）',
    `created_at`        DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `expires_at`        DATETIME        NOT NULL                     COMMENT '默认 created_at + 30 天',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_msg_id` (`wecom_msg_id`),
    KEY `idx_user_time` (`userid`, `created_at`),
    KEY `idx_expires`   (`expires_at`),
    KEY `idx_conversation_recommendation_delivery` (`recommendation_delivery_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='对话历史日志';


-- ============================================================================
-- 5. audit_log 审核动作日志
-- ============================================================================
DROP TABLE IF EXISTS `audit_log`;
CREATE TABLE `audit_log` (
    `id`           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `target_type`  ENUM('job','resume','user','system','recommendation_strategy') NOT NULL   COMMENT '审核对象类型（system=系统配置变更 / recommendation_strategy=推荐策略变更）',
    `target_id`    VARCHAR(64)     NOT NULL                         COMMENT 'job.id / resume.id / user.external_userid / 推荐策略方向',
    `action`       ENUM('auto_pass','auto_reject','manual_pass','manual_reject','manual_edit','undo','appeal','reinstate','strategy_publish','strategy_rollout','strategy_promote','strategy_rollback','strategy_kill_switch') NOT NULL,
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
    -- §9.10：默认取最小权限。phase9_004 只把"迁移前既有"的账号提升为 super_admin，
    -- 新建账号必须显式指定角色，不能静默继承全部控制权。
    -- 建库脚本的引导管理员由 seed.sql 显式写入 role='super_admin'。
    `role`           ENUM('viewer','operator','super_admin') NOT NULL DEFAULT 'viewer' COMMENT '管理员角色',
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
    `msg_type`           ENUM('text','image','voice','video','file','link','location','event','other') NOT NULL COMMENT '消息类型（原始企微 MsgType，一期仅 text/image/voice/event 走业务路径，其余走"不支持"分支）',
    `media_id`           VARCHAR(128)    DEFAULT NULL                 COMMENT '媒体消息的 media_id（image/voice/video/file 类型有效；crash 恢复时用于补下载）',
    `content_brief`      VARCHAR(500)    DEFAULT NULL                 COMMENT '消息摘要（文本取前 500 字）',
    `status`             ENUM('received','processing','session_pending','done','failed','dead_letter') NOT NULL DEFAULT 'received' COMMENT '处理状态',
    `retry_count`        TINYINT UNSIGNED NOT NULL DEFAULT 0          COMMENT '已重试次数',
    `session_operation`  VARCHAR(8)      DEFAULT NULL,
    `session_expected_version` INT UNSIGNED DEFAULT NULL,
    `session_payload`    JSON            DEFAULT NULL,
    `session_apply_attempts` INT UNSIGNED NOT NULL DEFAULT 0,
    `session_apply_locked_at` DATETIME(6) DEFAULT NULL,
    `session_next_attempt_at` DATETIME(6) DEFAULT NULL,
    `session_applied_at` DATETIME(6)     DEFAULT NULL,
    `worker_started_at`  DATETIME(6)     DEFAULT NULL                 COMMENT 'Worker 开始处理时间',
    `worker_finished_at` DATETIME(6)     DEFAULT NULL                 COMMENT 'Worker 处理完成时间',
    `error_message`      TEXT            DEFAULT NULL                 COMMENT '失败原因',
    `created_at`         DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '回调到达时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_msg_id`    (`msg_id`),
    KEY `idx_status_time`     (`status`, `created_at`),
    KEY `idx_status_worker_started`  (`status`, `worker_started_at`),
    KEY `idx_status_worker_finished` (`status`, `worker_finished_at`),
    KEY `idx_from_user`       (`from_userid`, `created_at`),
    KEY `idx_user_status_id`  (`from_userid`, `status`, `id`),
    KEY `idx_session_commit_due` (`status`, `session_next_attempt_at`, `session_apply_locked_at`, `id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='企微入站事件表';


-- ============================================================================
-- 12. wecom_outbound_outbox 企微回复事务出站箱
-- ============================================================================
DROP TABLE IF EXISTS `wecom_outbound_outbox`;
CREATE TABLE `wecom_outbound_outbox` (
    `id`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `inbound_event_id`  BIGINT UNSIGNED NOT NULL COMMENT '来源 wecom_inbound_event.id',
    `reply_index`       SMALLINT UNSIGNED NOT NULL COMMENT '同一入站事件内回复顺序',
    `userid`            VARCHAR(64) NOT NULL COMMENT '接收者 external_userid',
    `msg_type`          VARCHAR(16) NOT NULL DEFAULT 'text',
    -- 推荐回复只把正文留在加密的 recommendation_delivery 信封里，出站箱不再持有明文
    `content`           MEDIUMTEXT DEFAULT NULL,
    `intent`            VARCHAR(32) DEFAULT NULL,
    `criteria_snapshot` JSON DEFAULT NULL,
    `status`            ENUM('pending','sending','sent','dead_letter') NOT NULL DEFAULT 'pending',
    `attempt_count`     TINYINT UNSIGNED NOT NULL DEFAULT 0,
    `next_attempt_at`   DATETIME(6) DEFAULT NULL,
    `locked_at`         DATETIME(6) DEFAULT NULL,
    `provider_msg_id`   VARCHAR(128) DEFAULT NULL,
    `last_error`        TEXT DEFAULT NULL,
    `recommendation_delivery_id` CHAR(36) DEFAULT NULL COMMENT '关联的推荐投递（非推荐回复为 NULL）',
    `created_at`        DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    `sent_at`           DATETIME(6) DEFAULT NULL,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_outbox_event_reply` (`inbound_event_id`, `reply_index`),
    UNIQUE KEY `uk_outbox_recommendation_delivery` (`recommendation_delivery_id`),
    KEY `idx_outbox_status_due` (`status`, `next_attempt_at`, `id`),
    KEY `idx_outbox_status_locked` (`status`, `locked_at`),
    KEY `idx_outbox_event` (`inbound_event_id`, `id`),
    KEY `idx_outbox_user_status_id` (`userid`, `status`, `id`)
    -- fk_outbox_recommendation_delivery 在 recommendation_delivery 建表后统一补
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='企微回复事务出站箱';


-- ============================================================================
-- 13. event_log 外部事件回传日志（Phase 5 §5.9）
-- ============================================================================
-- 用途：接收小程序点击等外部事件回传，用于数据看板"详情点击率"指标
-- ============================================================================
DROP TABLE IF EXISTS `event_log`;
CREATE TABLE `event_log` (
    `id`          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `event_type`  ENUM('miniprogram_click') NOT NULL COMMENT '事件类型',
    `userid`      VARCHAR(64) NOT NULL COMMENT 'external_userid',
    `target_type` ENUM('job','resume') NOT NULL COMMENT '点击目标类型',
    `target_id`   BIGINT UNSIGNED NOT NULL COMMENT '目标主键',
    `occurred_at` DATETIME NOT NULL COMMENT '客户端上报的发生时间',
    `extra`       JSON DEFAULT NULL COMMENT '扩展字段',

    -- ---- 推荐归因字段（§9.9，phase9_003 / phase9_006）----
    `delivery_id`                    CHAR(36) DEFAULT NULL     COMMENT '归因到的推荐投递',
    `request_id`                     CHAR(36) DEFAULT NULL     COMMENT '归因到的推荐请求',
    `snapshot_id`                    CHAR(36) DEFAULT NULL     COMMENT '归因到的候选快照',
    `position`                       SMALLINT UNSIGNED DEFAULT NULL COMMENT '点击项在推荐列表中的位次',
    `attribution_status`             VARCHAR(24) NOT NULL DEFAULT 'legacy_unattributed' COMMENT 'attributed / legacy_unattributed / rejected',
    `attributed_strategy_version_id` BIGINT UNSIGNED DEFAULT NULL COMMENT '归因命中的策略版本',
    `attributed_algorithm_version`   VARCHAR(32) DEFAULT NULL  COMMENT '归因命中的算法版本',
    `attributed_is_exploration`      TINYINT(1) DEFAULT NULL   COMMENT '被点击项是否为探索位',
    `client_event_id`                VARCHAR(64) DEFAULT NULL  COMMENT '客户端幂等键',
    `attribution_dedupe_key`         CHAR(64) DEFAULT NULL     COMMENT '服务端归因去重键',

    `created_at`  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_event_attribution_dedupe` (`attribution_dedupe_key`),
    UNIQUE KEY `uk_event_client_idempotency` (`userid`, `event_type`, `client_event_id`),
    KEY `idx_target` (`target_type`, `target_id`, `occurred_at`),
    KEY `idx_user_time` (`userid`, `occurred_at`),
    KEY `idx_event_delivery_target` (`delivery_id`, `target_type`, `target_id`),
    KEY `idx_event_attributed_version` (`attributed_strategy_version_id`, `event_type`, `occurred_at`),
    KEY `idx_event_attribution_status` (`attribution_status`, `occurred_at`)
    -- fk_event_recommendation_delivery 在 recommendation_delivery 建表后统一补
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='外部事件回传日志';


-- ============================================================================
-- 14. recommendation_strategy_version 推荐策略版本（§9.1）
-- ============================================================================
-- 以下 9 张表对应 phase9_001..007 迁移的最终形态。schema.sql 是全新建库脚本，
-- 直接写目标结构，不需要迁移里的 information_schema 守卫。
-- ============================================================================
DROP TABLE IF EXISTS `recommendation_strategy_version`;
CREATE TABLE `recommendation_strategy_version` (
    `id`                    BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `direction`             VARCHAR(32)  NOT NULL                  COMMENT 'search_job / search_worker',
    `version_no`            INT UNSIGNED NOT NULL                  COMMENT '方向内自增版本号',
    `template_key`          VARCHAR(32)  NOT NULL                  COMMENT '参数模板标识',
    `status`                ENUM('draft','published','archived') NOT NULL DEFAULT 'draft',
    `parameters`            JSON         NOT NULL                  COMMENT '策略参数全量快照',
    `parameters_digest`     CHAR(64)     NOT NULL                  COMMENT 'parameters 的 SHA256',
    `last_simulated_digest` CHAR(64)     DEFAULT NULL              COMMENT '最近一次仿真所用参数摘要',
    `last_simulated_at`     DATETIME(6)  DEFAULT NULL,
    `algorithm_version`     VARCHAR(32)  NOT NULL DEFAULT 'recommendation-v1',
    `base_version_id`       BIGINT UNSIGNED DEFAULT NULL           COMMENT '派生自哪个版本',
    `lock_version`          INT UNSIGNED NOT NULL DEFAULT 1        COMMENT '乐观锁版本号',
    `change_reason`         VARCHAR(255) NOT NULL                  COMMENT '变更原因（必填）',
    `created_by`            VARCHAR(64)  NOT NULL,
    `created_at`            DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    `published_by`          VARCHAR(64)  DEFAULT NULL,
    `published_at`          DATETIME(6)  DEFAULT NULL,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_recommendation_version_direction_no` (`direction`, `version_no`),
    KEY `idx_recommendation_version_status` (`direction`, `status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='推荐策略版本';


-- ============================================================================
-- 15. recommendation_strategy_release 方向级发布状态（§9.2）
-- ============================================================================
DROP TABLE IF EXISTS `recommendation_strategy_release`;
CREATE TABLE `recommendation_strategy_release` (
    `direction`             VARCHAR(32)  NOT NULL                  COMMENT 'search_job / search_worker',
    `execution_mode`        ENUM('off','shadow','on') NOT NULL DEFAULT 'off',
    `stable_version_id`     BIGINT UNSIGNED DEFAULT NULL           COMMENT '稳定版本（NULL=legacy 基线）',
    `candidate_version_id`  BIGINT UNSIGNED DEFAULT NULL           COMMENT '灰度候选版本',
    `rollout_percentage`    INT UNSIGNED NOT NULL DEFAULT 0        COMMENT '候选版本灰度比例 0-100',
    `revision`              BIGINT UNSIGNED NOT NULL DEFAULT 1     COMMENT '发布修订号，与 history 对齐',
    `lock_version`          INT UNSIGNED NOT NULL DEFAULT 1        COMMENT '乐观锁版本号',
    `updated_by`            VARCHAR(64)  NOT NULL DEFAULT 'system',
    `updated_at`            DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (`direction`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='推荐策略方向级发布状态';


-- ============================================================================
-- 16. recommendation_release_history 发布操作台账（§9.2）
-- ============================================================================
DROP TABLE IF EXISTS `recommendation_release_history`;
CREATE TABLE `recommendation_release_history` (
    `direction`             VARCHAR(32)  NOT NULL,
    `revision`              BIGINT UNSIGNED NOT NULL               COMMENT '与 release.revision 一一对应',
    `operation`             VARCHAR(32)  NOT NULL                  COMMENT 'init/publish/rollout/promote/rollback/kill_switch',
    `execution_mode`        VARCHAR(16)  NOT NULL                  COMMENT '本次操作后的执行模式',
    `stable_version_id`     BIGINT UNSIGNED DEFAULT NULL,
    `candidate_version_id`  BIGINT UNSIGNED DEFAULT NULL,
    `rollout_percentage`    INT UNSIGNED NOT NULL,
    `target_revision`       BIGINT UNSIGNED DEFAULT NULL           COMMENT '回滚目标 revision',
    `change_reason`         VARCHAR(255) NOT NULL,
    `created_by`            VARCHAR(64)  NOT NULL,
    `created_at`            DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (`direction`, `revision`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='推荐策略发布操作台账';


-- ============================================================================
-- 17. recommendation_runtime_control 运行时总闸（§9.2）
-- ============================================================================
DROP TABLE IF EXISTS `recommendation_runtime_control`;
CREATE TABLE `recommendation_runtime_control` (
    `scope`          VARCHAR(16)  NOT NULL                         COMMENT '一期固定 global',
    `kill_switch`    TINYINT(1)   NOT NULL DEFAULT 0               COMMENT '1=全局熔断回落 legacy',
    `revision`       BIGINT UNSIGNED NOT NULL DEFAULT 1,
    `lock_version`   INT UNSIGNED NOT NULL DEFAULT 1               COMMENT '乐观锁版本号',
    `change_reason`  VARCHAR(255) NOT NULL DEFAULT 'initial',
    `updated_by`     VARCHAR(64)  NOT NULL DEFAULT 'system',
    `updated_at`     DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (`scope`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='推荐运行时总闸';


-- ============================================================================
-- 18. recommendation_request 推荐请求事实表（§9.3）
-- ============================================================================
-- 注：parent_request_id 不额外建 idx_recommendation_request_parent，
--     InnoDB 会用与约束同名的索引回填 fk_recommendation_request_parent，
--     phase9_007 在这种情况下同样是空操作，避免重复索引。
-- ============================================================================
DROP TABLE IF EXISTS `recommendation_request`;
CREATE TABLE `recommendation_request` (
    `request_id`                    CHAR(36)     NOT NULL,
    `source_inbound_msg_id`         VARCHAR(64)  NOT NULL          COMMENT '来源企微消息 ID',
    `request_index`                 SMALLINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '同一入站消息内的请求序号',
    `request_kind`                  VARCHAR(32)  NOT NULL          COMMENT 'initial / auto_relax / show_more',
    `parent_request_id`             CHAR(36)     DEFAULT NULL      COMMENT '放宽/翻页的父请求',
    `served_attempt_id`             CHAR(36)     DEFAULT NULL      COMMENT '最终采纳的检索尝试',
    `snapshot_id`                   CHAR(36)     DEFAULT NULL      COMMENT '候选快照 ID',
    `viewer_userid`                 VARCHAR(64)  NOT NULL,
    `direction`                     VARCHAR(32)  NOT NULL,
    `query_digest`                  VARCHAR(16)  NOT NULL          COMMENT '查询条件短摘要',
    `execution_mode`                VARCHAR(16)  NOT NULL          COMMENT '本次请求生效的执行模式',
    `served_assignment`             VARCHAR(16)  NOT NULL          COMMENT 'legacy / stable / candidate',
    `served_strategy_version_id`    BIGINT UNSIGNED DEFAULT NULL,
    `candidate_strategy_version_id` BIGINT UNSIGNED DEFAULT NULL,
    `algorithm_version`             VARCHAR(32)  NOT NULL,
    `final_candidate_count`         INT UNSIGNED NOT NULL DEFAULT 0,
    `result_count`                  INT UNSIGNED NOT NULL DEFAULT 0,
    `is_zero_result`                TINYINT(1)   NOT NULL DEFAULT 0,
    `show_more_exhausted`           TINYINT(1)   NOT NULL DEFAULT 0,
    `total_latency_ms`              INT UNSIGNED NOT NULL DEFAULT 0,
    `served_top_ids`                JSON         NOT NULL          COMMENT '最终返回的目标 ID 序列',
    `served_owner_count`            INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '返回结果覆盖的发布者数',
    `served_max_owner_items`        INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '单一发布者最多占几条',
    `served_exploration_count`      INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '返回结果中的探索位数量',
    `shadow_top_ids`                JSON NULL COMMENT 'shadow 候选 Top N',
    `shadow_overlap_count`          INT UNSIGNED NULL COMMENT '与 legacy Top N 重合数',
    `shadow_rank_delta`             JSON NULL COMMENT '共同候选位次差',
    `shadow_status`                 VARCHAR(32) NULL COMMENT 'completed/timeout/timeout_in_queue/skipped_capacity/failed',
    `shadow_queue_wait_ms`          INT UNSIGNED NULL COMMENT 'shadow runner 排队时间',
    `shadow_latency_ms`             INT UNSIGNED NULL COMMENT 'shadow 增量计算耗时',
    `shadow_input_tokens`           INT UNSIGNED NULL COMMENT 'shadow 输入 token',
    `shadow_output_tokens`          INT UNSIGNED NULL COMMENT 'shadow 输出 token',
    `shadow_fallback`               VARCHAR(32) NULL COMMENT 'shadow 回退类型',
    `created_at`                    DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (`request_id`),
    UNIQUE KEY `uk_recommendation_request_inbound_index` (`source_inbound_msg_id`, `request_index`),
    KEY `idx_recommendation_request_viewer_time` (`viewer_userid`, `direction`, `created_at`),
    KEY `idx_recommendation_request_attempt` (`served_attempt_id`),
    KEY `idx_recommendation_request_mode_time` (`created_at`, `direction`, `execution_mode`),
    KEY `idx_recommendation_request_kind_zero` (`request_kind`, `is_zero_result`, `created_at`),
    KEY `idx_recommendation_request_version_time` (`served_strategy_version_id`, `created_at`),
    CONSTRAINT `fk_recommendation_request_parent`
        FOREIGN KEY (`parent_request_id`) REFERENCES `recommendation_request`(`request_id`) ON DELETE SET NULL
    -- fk_recommendation_request_served_attempt 是循环引用，见文件末尾的 ALTER
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='推荐请求事实表';


-- ============================================================================
-- 19. recommendation_search_attempt 检索尝试事实表（§9.4）
-- ============================================================================
DROP TABLE IF EXISTS `recommendation_search_attempt`;
CREATE TABLE `recommendation_search_attempt` (
    `attempt_id`             CHAR(36)     NOT NULL,
    `request_id`             CHAR(36)     NOT NULL,
    `attempt_no`             SMALLINT UNSIGNED NOT NULL            COMMENT '请求内尝试序号，从 0 递增',
    `attempt_kind`           VARCHAR(32)  NOT NULL                 COMMENT 'initial / relax_probe / auto_relaxed / confirmed_relaxed / shadow_candidate',
    `criteria_digest`        CHAR(64)     NOT NULL                 COMMENT '本次检索条件的 SHA256',
    `scoring_time_utc`       DATETIME(6)  NOT NULL                 COMMENT '打分基准时间（复现用）',
    `candidate_count`        INT UNSIGNED NOT NULL DEFAULT 0,
    `candidate_ids`          JSON         NOT NULL                 COMMENT '硬过滤后的候选 ID',
    `precision_pool_ids`     JSON         NOT NULL                 COMMENT '精排池 ID',
    `result_count`           INT UNSIGNED NOT NULL DEFAULT 0,
    `is_zero_result`         TINYINT(1)   NOT NULL DEFAULT 0,
    `strategy_version_id`    BIGINT UNSIGNED DEFAULT NULL,
    `algorithm_version`      VARCHAR(32)  NOT NULL,
    `llm_status`             VARCHAR(32)  NOT NULL DEFAULT 'skipped' COMMENT 'skipped/ok/timeout/error/fallback',
    `llm_input_tokens`       INT UNSIGNED DEFAULT NULL,
    `llm_output_tokens`      INT UNSIGNED DEFAULT NULL,
    `llm_timeout_budget_ms`  INT UNSIGNED DEFAULT NULL             COMMENT '本次尝试分配的 LLM 超时预算',
    `llm_retry_count`        SMALLINT UNSIGNED NOT NULL DEFAULT 0  COMMENT 'LLM 重试次数',
    `ranking_fallback`       VARCHAR(32)  DEFAULT NULL             COMMENT '回落到的排序方式',
    `ranking_latency_ms`     INT UNSIGNED NOT NULL DEFAULT 0,
    `total_latency_ms`       INT UNSIGNED NOT NULL DEFAULT 0,
    `created_at`             DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (`attempt_id`),
    UNIQUE KEY `uk_recommendation_attempt_request_no` (`request_id`, `attempt_no`),
    KEY `idx_recommendation_attempt_kind_time` (`created_at`, `attempt_kind`),
    KEY `idx_recommendation_attempt_version_time` (`strategy_version_id`, `created_at`),
    KEY `idx_recommendation_attempt_llm_status` (`llm_status`, `created_at`),
    CONSTRAINT `fk_recommendation_attempt_request`
        FOREIGN KEY (`request_id`) REFERENCES `recommendation_request`(`request_id`) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='推荐检索尝试事实表';


-- ============================================================================
-- 20. recommendation_delivery 推荐投递出站箱（§9.5 / §9.6）
-- ============================================================================
DROP TABLE IF EXISTS `recommendation_delivery`;
CREATE TABLE `recommendation_delivery` (
    `delivery_id`                  CHAR(36)     NOT NULL,
    `delivery_order`               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT UNIQUE COMMENT '全局单调序号，供 show_more 定位',
    `source_inbound_msg_id`        VARCHAR(64)  NOT NULL,
    `reply_index`                  SMALLINT UNSIGNED NOT NULL,
    `request_id`                   CHAR(36)     NOT NULL,
    `snapshot_id`                  CHAR(36)     DEFAULT NULL,
    `userid`                       VARCHAR(64)  NOT NULL           COMMENT '接收者 external_userid',

    -- ---- 加密正文信封 ----
    `content_ciphertext`           MEDIUMBLOB   DEFAULT NULL       COMMENT '回复正文密文',
    `content_key_version`          SMALLINT UNSIGNED NOT NULL DEFAULT 1,
    `content_hash`                 CHAR(64)     DEFAULT NULL,
    `content_expires_at`           DATETIME(6)  DEFAULT NULL       COMMENT '正文 TTL（phase9_005）',
    `recommendation_context`       JSON         NOT NULL           COMMENT '归因所需的上下文（不含明文正文）',
    `status`                       VARCHAR(24)  NOT NULL DEFAULT 'prepared',

    -- ---- 会话补偿提交 ----
    `session_expected_version`     BIGINT UNSIGNED NOT NULL DEFAULT 0,
    `session_commit_token`         CHAR(36)     NOT NULL,
    `session_patch_ciphertext`     MEDIUMBLOB   DEFAULT NULL,
    `session_commit_state`         VARCHAR(16)  NOT NULL DEFAULT 'not_applied',
    `session_committed_at`         DATETIME(6)  DEFAULT NULL,

    -- ---- 发送重试与租约 ----
    `attempt_count`                INT UNSIGNED NOT NULL DEFAULT 0,
    `next_attempt_at`              DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    `lease_owner`                  VARCHAR(64)  DEFAULT NULL,
    `lease_expires_at`             DATETIME(6)  DEFAULT NULL,
    `wecom_msgid`                  VARCHAR(128) DEFAULT NULL,
    `wecom_response`               JSON         DEFAULT NULL,
    `invalid_recipients`           JSON         DEFAULT NULL       COMMENT '企微返回的无效接收人',
    `last_error_code`              VARCHAR(32)  DEFAULT NULL,
    `last_error`                   VARCHAR(500) DEFAULT NULL,
    `sent_at`                      DATETIME(6)  DEFAULT NULL,

    -- ---- 曝光派生 ----
    `impression_state`             VARCHAR(24)  NOT NULL DEFAULT 'pending',
    `impression_expected_count`    SMALLINT UNSIGNED NOT NULL DEFAULT 0,
    `impression_actual_count`      SMALLINT UNSIGNED NOT NULL DEFAULT 0,
    `impression_attempt_count`     INT UNSIGNED NOT NULL DEFAULT 0,
    `impression_next_attempt_at`   DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    `impression_lease_owner`       VARCHAR(64)  DEFAULT NULL,
    `impression_lease_expires_at`  DATETIME(6)  DEFAULT NULL,
    `impression_derived_at`        DATETIME(6)  DEFAULT NULL,
    `impression_last_error`        VARCHAR(500) DEFAULT NULL,

    `created_at`                   DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    `updated_at`                   DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (`delivery_id`),
    UNIQUE KEY `uk_recommendation_delivery_inbound_index` (`source_inbound_msg_id`, `reply_index`),
    KEY `idx_recommendation_delivery_user_order` (`userid`, `delivery_order`),
    KEY `idx_recommendation_delivery_status_due` (`status`, `next_attempt_at`),
    KEY `idx_recommendation_delivery_session_recovery` (`status`, `session_commit_state`, `updated_at`),
    KEY `idx_recommendation_delivery_user_status_order` (`userid`, `status`, `delivery_order`),
    KEY `idx_recommendation_delivery_lease` (`lease_expires_at`, `status`),
    KEY `idx_recommendation_delivery_impression_due` (`status`, `impression_state`, `impression_next_attempt_at`),
    KEY `idx_recommendation_delivery_impression_lease` (`impression_lease_expires_at`, `impression_state`),
    KEY `idx_recommendation_delivery_request` (`request_id`),
    KEY `idx_recommendation_delivery_msgid` (`wecom_msgid`),
    CONSTRAINT `fk_recommendation_delivery_request`
        FOREIGN KEY (`request_id`) REFERENCES `recommendation_request`(`request_id`) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='推荐投递出站箱';


-- ============================================================================
-- 21. recommendation_impression 曝光事实表（§9.7）
-- ============================================================================
DROP TABLE IF EXISTS `recommendation_impression`;
CREATE TABLE `recommendation_impression` (
    `id`                    BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `delivery_id`           CHAR(36)     NOT NULL,
    `request_id`            CHAR(36)     NOT NULL,
    `snapshot_id`           CHAR(36)     NOT NULL,
    `viewer_userid`         VARCHAR(64)  NOT NULL,
    `direction`             VARCHAR(32)  NOT NULL,
    `target_type`           VARCHAR(16)  NOT NULL                  COMMENT 'job / resume',
    `target_id`             BIGINT UNSIGNED NOT NULL,
    `position`              SMALLINT UNSIGNED NOT NULL             COMMENT '曝光位次，从 1 起',
    `strategy_version_id`   BIGINT UNSIGNED DEFAULT NULL,
    `algorithm_version`     VARCHAR(32)  NOT NULL,
    `assignment`            VARCHAR(16)  NOT NULL                  COMMENT 'legacy / stable / candidate',
    `is_exploration`        TINYINT(1)   NOT NULL DEFAULT 0,
    `query_digest`          VARCHAR(16)  NOT NULL,
    `score_detail`          JSON         DEFAULT NULL              COMMENT '分项打分明细',
    `exposed_at`            DATETIME(6)  NOT NULL,
    `created_at`            DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_recommendation_impression_delivery_target` (`delivery_id`, `target_type`, `target_id`),
    KEY `idx_recommendation_impression_viewer_time` (`viewer_userid`, `target_type`, `exposed_at`),
    KEY `idx_recommendation_impression_target_time` (`target_type`, `target_id`, `exposed_at`),
    KEY `idx_recommendation_impression_version_time` (`strategy_version_id`, `exposed_at`),
    KEY `idx_recommendation_impression_snapshot_position` (`snapshot_id`, `position`),
    -- §9.11：曝光随投递级联删除，但钉住 request
    CONSTRAINT `fk_recommendation_impression_delivery`
        FOREIGN KEY (`delivery_id`) REFERENCES `recommendation_delivery`(`delivery_id`) ON DELETE CASCADE,
    CONSTRAINT `fk_recommendation_impression_request`
        FOREIGN KEY (`request_id`) REFERENCES `recommendation_request`(`request_id`) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='推荐曝光事实表';


-- ============================================================================
-- 22. recommendation_exposure_daily 曝光日聚合（§9.8）
-- ============================================================================
DROP TABLE IF EXISTS `recommendation_exposure_daily`;
CREATE TABLE `recommendation_exposure_daily` (
    `stat_date`        DATE         NOT NULL,
    `target_type`      VARCHAR(16)  NOT NULL,
    `target_id`        BIGINT UNSIGNED NOT NULL,
    `impression_count` INT UNSIGNED NOT NULL DEFAULT 0,
    `updated_at`       DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (`stat_date`, `target_type`, `target_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='推荐曝光日聚合';


-- ============================================================================
-- 推荐相关的延后外键（引用的表在上面才建出来）
-- ============================================================================
ALTER TABLE `recommendation_request`
    ADD CONSTRAINT `fk_recommendation_request_served_attempt`
    FOREIGN KEY (`served_attempt_id`) REFERENCES `recommendation_search_attempt`(`attempt_id`)
    ON DELETE SET NULL;

ALTER TABLE `wecom_outbound_outbox`
    ADD CONSTRAINT `fk_outbox_recommendation_delivery`
    FOREIGN KEY (`recommendation_delivery_id`) REFERENCES `recommendation_delivery`(`delivery_id`)
    ON DELETE SET NULL;

ALTER TABLE `event_log`
    ADD CONSTRAINT `fk_event_recommendation_delivery`
    FOREIGN KEY (`delivery_id`) REFERENCES `recommendation_delivery`(`delivery_id`)
    ON DELETE SET NULL;


SET FOREIGN_KEY_CHECKS = 1;

-- ============================================================================
-- Phase 5 新增/变更 DDL（已存在库需单独执行 ALTER）
-- ============================================================================
-- audit_log.action 枚举扩展：新增 manual_edit / undo
--   ALTER TABLE audit_log MODIFY COLUMN action
--     ENUM('auto_pass','auto_reject','manual_pass','manual_reject',
--          'manual_edit','undo','appeal','reinstate') NOT NULL;
-- audit_log.target_type 枚举扩展：新增 system（承载系统配置变更审计）
--   ALTER TABLE audit_log MODIFY COLUMN target_type
--     ENUM('job','resume','user','system') NOT NULL;
-- event_log 新表：见上
-- ============================================================================

-- ============================================================================
-- 推荐策略与曝光多样性 v1 DDL（已存在库不要跑本文件）
-- ============================================================================
-- 本文件是"全新建库"脚本，第 14~22 张表已经是 phase9_001..007 的最终形态。
-- 已有生产库必须走带台账的迁移脚本，禁止手工 ALTER：
--   cd backend
--   python scripts/apply_phase9_migrations.py --dsn-env DB_URL \
--     --manifest sql/migrations/phase9_manifest.sha256 --check
--   python scripts/apply_phase9_migrations.py --dsn-env DB_URL \
--     --manifest sql/migrations/phase9_manifest.sha256 --apply
--   python scripts/apply_phase9_migrations.py --dsn-env DB_URL \
--     --manifest sql/migrations/phase9_manifest.sha256 --verify
-- 回滚（仅限新表为空且功能未启用时）按 phase9_down_007 → phase9_down_001 逆序
-- 手工执行，down 文件不在 manifest 里。
-- ============================================================================

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
