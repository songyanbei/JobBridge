-- Job Search v1 Action idempotency and lease/fencing contract.
-- Additive and safe to run repeatedly on MySQL 8.0.

CREATE TABLE IF NOT EXISTS `action_execution` (
    `id`             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `turn_id`        CHAR(36)        NOT NULL COMMENT '不可变入站轮次 ID',
    `action_name`    VARCHAR(64)     NOT NULL COMMENT '稳定 Action 名称',
    `status`         ENUM('started','succeeded','failed_retryable','failed_terminal')
                     NOT NULL DEFAULT 'started' COMMENT 'Action 执行状态',
    `request_digest` CHAR(64)        DEFAULT NULL COMMENT '规范化请求 SHA-256',
    `result_digest`  CHAR(64)        DEFAULT NULL COMMENT '结果/快照 SHA-256',
    `lease_owner`    VARCHAR(64)     DEFAULT NULL COMMENT '当前 Worker owner',
    `lease_until`    DATETIME(6)     DEFAULT NULL COMMENT '当前 lease 到期时间；过期后才可抢占',
    `fencing_token`  BIGINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '每次过期抢占递增的 fencing token',
    `created_at`     DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    `finished_at`    DATETIME(6)     DEFAULT NULL,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_action_execution_turn_action` (`turn_id`,`action_name`),
    KEY `idx_action_execution_claim` (`status`,`lease_until`,`id`),
    KEY `idx_action_execution_turn` (`turn_id`,`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='Action 幂等执行与 lease/fencing 凭据';
