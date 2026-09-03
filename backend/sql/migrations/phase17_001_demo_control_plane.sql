-- Phase 17: isolated demo-mode control plane.
-- Additive only: this migration never changes User.role, AIBot bindings, or
-- existing business data.  Run after the Phase 16 identity-role-binding
-- migration on MySQL 8.0+.

CREATE TABLE IF NOT EXISTS `demo_workspace` (
  `demo_id` VARCHAR(64) NOT NULL,
  `name` VARCHAR(128) NOT NULL,
  `status` ENUM('active','disabled','cleaning','cleaned','failed') NOT NULL DEFAULT 'active',
  `bot_id` VARCHAR(128) NOT NULL,
  `opaque_actor_digest` CHAR(64) NOT NULL,
  `canonical_actor_userid` VARCHAR(64) DEFAULT NULL,
  `created_by` VARCHAR(64) NOT NULL,
  `reason` VARCHAR(255) DEFAULT NULL,
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `disabled_at` DATETIME(6) DEFAULT NULL,
  `cleaned_at` DATETIME(6) DEFAULT NULL,
  `updated_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`demo_id`),
  KEY `idx_demo_workspace_status` (`status`,`created_at`),
  KEY `idx_demo_workspace_bot_actor` (`bot_id`,`opaque_actor_digest`,`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='演示模式工作区控制面';

CREATE TABLE IF NOT EXISTS `demo_workspace_member` (
  `member_id` CHAR(36) NOT NULL,
  `demo_id` VARCHAR(64) NOT NULL,
  `bot_id` VARCHAR(128) NOT NULL,
  `opaque_actor_digest` CHAR(64) NOT NULL,
  `canonical_actor_userid` VARCHAR(64) DEFAULT NULL,
  `membership_status` ENUM('active','revoked','expired') NOT NULL DEFAULT 'active',
  `granted_by` VARCHAR(64) NOT NULL,
  `expires_at` DATETIME(6) DEFAULT NULL,
  `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `revoked_at` DATETIME(6) DEFAULT NULL,
  `updated_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`member_id`),
  UNIQUE KEY `uk_demo_member_actor` (`demo_id`,`bot_id`,`opaque_actor_digest`),
  KEY `idx_demo_member_lookup` (`bot_id`,`opaque_actor_digest`,`membership_status`),
  KEY `idx_demo_member_workspace` (`demo_id`,`membership_status`),
  CONSTRAINT `fk_demo_member_workspace` FOREIGN KEY (`demo_id`)
    REFERENCES `demo_workspace` (`demo_id`) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='演示工作区授权成员';

CREATE TABLE IF NOT EXISTS `demo_principal` (
  `principal_id` CHAR(36) NOT NULL,
  `demo_id` VARCHAR(64) NOT NULL,
  `role` ENUM('worker','factory','broker') NOT NULL,
  `synthetic_userid` VARCHAR(64) NOT NULL,
  `principal_status` ENUM('active','revoked') NOT NULL DEFAULT 'active',
  `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `revoked_at` DATETIME(6) DEFAULT NULL,
  `updated_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`principal_id`),
  UNIQUE KEY `uk_demo_principal_role` (`demo_id`,`role`),
  UNIQUE KEY `uk_demo_principal_user` (`synthetic_userid`),
  KEY `idx_demo_principal_workspace_status` (`demo_id`,`principal_status`),
  CONSTRAINT `fk_demo_principal_workspace` FOREIGN KEY (`demo_id`)
    REFERENCES `demo_workspace` (`demo_id`) ON DELETE RESTRICT,
  CONSTRAINT `fk_demo_principal_user` FOREIGN KEY (`synthetic_userid`)
    REFERENCES `user` (`external_userid`) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='演示工作区业务主体';

CREATE TABLE IF NOT EXISTS `demo_resource` (
  `resource_id` CHAR(36) NOT NULL,
  `demo_id` VARCHAR(64) NOT NULL,
  `resource_type` VARCHAR(48) NOT NULL,
  `target_id` VARCHAR(128) NOT NULL,
  `lifecycle_status` ENUM('active','delisted','cleaning','cleaned','failed') NOT NULL DEFAULT 'active',
  `metadata` JSON DEFAULT NULL,
  `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `cleaned_at` DATETIME(6) DEFAULT NULL,
  `last_error` VARCHAR(255) DEFAULT NULL,
  PRIMARY KEY (`resource_id`),
  UNIQUE KEY `uk_demo_resource_target` (`demo_id`,`resource_type`,`target_id`),
  KEY `idx_demo_resource_cleanup` (`demo_id`,`lifecycle_status`,`resource_type`),
  CONSTRAINT `fk_demo_resource_workspace` FOREIGN KEY (`demo_id`)
    REFERENCES `demo_workspace` (`demo_id`) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='演示资源清单';

-- No seed rows are inserted here.  A workspace must be provisioned by the
-- control-plane service so actor authorization and synthetic principals are
-- created atomically and audited.
