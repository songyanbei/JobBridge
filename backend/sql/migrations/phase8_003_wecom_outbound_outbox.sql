-- Phase 8: durable transactional outbox for WeCom replies.
-- Idempotent for repeated deployment runs.

CREATE TABLE IF NOT EXISTS `wecom_outbound_outbox` (
    `id`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `inbound_event_id`  BIGINT UNSIGNED NOT NULL COMMENT '来源 wecom_inbound_event.id',
    `reply_index`       SMALLINT UNSIGNED NOT NULL COMMENT '同一入站事件内回复顺序',
    `userid`            VARCHAR(64) NOT NULL COMMENT '接收者 external_userid',
    `msg_type`          VARCHAR(16) NOT NULL DEFAULT 'text',
    `content`           MEDIUMTEXT NOT NULL,
    `intent`            VARCHAR(32) DEFAULT NULL,
    `criteria_snapshot` JSON DEFAULT NULL,
    `status`            ENUM('pending','sending','sent','dead_letter') NOT NULL DEFAULT 'pending',
    `attempt_count`     TINYINT UNSIGNED NOT NULL DEFAULT 0,
    `next_attempt_at`   DATETIME(6) DEFAULT NULL,
    `locked_at`         DATETIME(6) DEFAULT NULL,
    `provider_msg_id`   VARCHAR(128) DEFAULT NULL,
    `last_error`        TEXT DEFAULT NULL,
    `created_at`        DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    `sent_at`           DATETIME(6) DEFAULT NULL,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_outbox_event_reply` (`inbound_event_id`, `reply_index`),
    KEY `idx_outbox_status_due` (`status`, `next_attempt_at`, `id`),
    KEY `idx_outbox_status_locked` (`status`, `locked_at`),
    KEY `idx_outbox_event` (`inbound_event_id`, `id`),
    KEY `idx_outbox_user_status_id` (`userid`, `status`, `id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='企微回复事务出站箱';
