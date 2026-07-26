-- Phase 9: recommendation request/attempt facts and the durable delivery outbox.
-- MySQL 8.0 compatible and safe to apply repeatedly.  `ADD COLUMN IF NOT EXISTS`
-- is MariaDB-only syntax, so existing-table changes use the same guarded
-- PREPARE/EXECUTE pattern as phase8_005.

SET @schema_name = DATABASE();

CREATE TABLE IF NOT EXISTS recommendation_request (
  request_id CHAR(36) NOT NULL,
  source_inbound_msg_id VARCHAR(64) NOT NULL,
  request_index SMALLINT UNSIGNED NOT NULL DEFAULT 0,
  request_kind VARCHAR(32) NOT NULL,
  parent_request_id CHAR(36) NULL,
  served_attempt_id CHAR(36) NULL,
  snapshot_id CHAR(36) NULL,
  viewer_userid VARCHAR(64) NOT NULL,
  direction VARCHAR(32) NOT NULL,
  query_digest VARCHAR(16) NOT NULL,
  execution_mode VARCHAR(16) NOT NULL,
  served_assignment VARCHAR(16) NOT NULL,
  served_strategy_version_id BIGINT UNSIGNED NULL,
  candidate_strategy_version_id BIGINT UNSIGNED NULL,
  algorithm_version VARCHAR(32) NOT NULL,
  final_candidate_count INT UNSIGNED NOT NULL DEFAULT 0,
  result_count INT UNSIGNED NOT NULL DEFAULT 0,
  is_zero_result TINYINT(1) NOT NULL DEFAULT 0,
  show_more_exhausted TINYINT(1) NOT NULL DEFAULT 0,
  total_latency_ms INT UNSIGNED NOT NULL DEFAULT 0,
  served_top_ids JSON NOT NULL,
  served_owner_count INT UNSIGNED NOT NULL DEFAULT 0,
  served_max_owner_items INT UNSIGNED NOT NULL DEFAULT 0,
  served_exploration_count INT UNSIGNED NOT NULL DEFAULT 0,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (request_id),
  UNIQUE KEY uk_recommendation_request_inbound_index (source_inbound_msg_id, request_index),
  KEY idx_recommendation_request_viewer_time (viewer_userid, direction, created_at),
  KEY idx_recommendation_request_attempt (served_attempt_id),
  KEY idx_recommendation_request_mode_time (created_at, direction, execution_mode),
  KEY idx_recommendation_request_kind_zero (request_kind, is_zero_result, created_at),
  KEY idx_recommendation_request_version_time (served_strategy_version_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS recommendation_search_attempt (
  attempt_id CHAR(36) NOT NULL,
  request_id CHAR(36) NOT NULL,
  attempt_no SMALLINT UNSIGNED NOT NULL,
  attempt_kind VARCHAR(32) NOT NULL,
  criteria_digest CHAR(64) NOT NULL,
  scoring_time_utc DATETIME(6) NOT NULL,
  candidate_count INT UNSIGNED NOT NULL DEFAULT 0,
  candidate_ids JSON NOT NULL,
  precision_pool_ids JSON NOT NULL,
  result_count INT UNSIGNED NOT NULL DEFAULT 0,
  is_zero_result TINYINT(1) NOT NULL DEFAULT 0,
  strategy_version_id BIGINT UNSIGNED NULL,
  algorithm_version VARCHAR(32) NOT NULL,
  llm_status VARCHAR(32) NOT NULL DEFAULT 'skipped',
  llm_input_tokens INT UNSIGNED NULL,
  llm_output_tokens INT UNSIGNED NULL,
  llm_timeout_budget_ms INT UNSIGNED NULL,
  llm_retry_count SMALLINT UNSIGNED NOT NULL DEFAULT 0,
  ranking_fallback VARCHAR(32) NULL,
  ranking_latency_ms INT UNSIGNED NOT NULL DEFAULT 0,
  total_latency_ms INT UNSIGNED NOT NULL DEFAULT 0,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (attempt_id),
  UNIQUE KEY uk_recommendation_attempt_request_no (request_id, attempt_no),
  KEY idx_recommendation_attempt_kind_time (created_at, attempt_kind),
  KEY idx_recommendation_attempt_version_time (strategy_version_id, created_at),
  KEY idx_recommendation_attempt_llm_status (llm_status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS recommendation_delivery (
  delivery_id CHAR(36) NOT NULL,
  delivery_order BIGINT UNSIGNED NOT NULL AUTO_INCREMENT UNIQUE,
  source_inbound_msg_id VARCHAR(64) NOT NULL,
  reply_index SMALLINT UNSIGNED NOT NULL,
  request_id CHAR(36) NOT NULL,
  snapshot_id CHAR(36) NULL,
  userid VARCHAR(64) NOT NULL,
  content_ciphertext MEDIUMBLOB NULL,
  content_key_version SMALLINT UNSIGNED NOT NULL DEFAULT 1,
  content_hash CHAR(64) NULL,
  recommendation_context JSON NOT NULL,
  status VARCHAR(24) NOT NULL DEFAULT 'prepared',
  session_expected_version BIGINT UNSIGNED NOT NULL DEFAULT 0,
  session_commit_token CHAR(36) NOT NULL,
  session_patch_ciphertext MEDIUMBLOB NULL,
  session_commit_state VARCHAR(16) NOT NULL DEFAULT 'not_applied',
  session_committed_at DATETIME(6) NULL,
  attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
  next_attempt_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  lease_owner VARCHAR(64) NULL,
  lease_expires_at DATETIME(6) NULL,
  wecom_msgid VARCHAR(128) NULL,
  wecom_response JSON NULL,
  invalid_recipients JSON NULL,
  last_error_code VARCHAR(32) NULL,
  last_error VARCHAR(500) NULL,
  sent_at DATETIME(6) NULL,
  impression_state VARCHAR(24) NOT NULL DEFAULT 'pending',
  impression_expected_count SMALLINT UNSIGNED NOT NULL DEFAULT 0,
  impression_actual_count SMALLINT UNSIGNED NOT NULL DEFAULT 0,
  impression_attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
  impression_next_attempt_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  impression_lease_owner VARCHAR(64) NULL,
  impression_lease_expires_at DATETIME(6) NULL,
  impression_derived_at DATETIME(6) NULL,
  impression_last_error VARCHAR(500) NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (delivery_id),
  UNIQUE KEY uk_recommendation_delivery_inbound_index (source_inbound_msg_id, reply_index),
  KEY idx_recommendation_delivery_user_order (userid, delivery_order),
  KEY idx_recommendation_delivery_status_due (status, next_attempt_at),
  KEY idx_recommendation_delivery_session_recovery (status, session_commit_state, updated_at),
  KEY idx_recommendation_delivery_user_status_order (userid, status, delivery_order),
  KEY idx_recommendation_delivery_lease (lease_expires_at, status),
  KEY idx_recommendation_delivery_impression_due (status, impression_state, impression_next_attempt_at),
  KEY idx_recommendation_delivery_impression_lease (impression_lease_expires_at, impression_state),
  KEY idx_recommendation_delivery_request (request_id),
  KEY idx_recommendation_delivery_msgid (wecom_msgid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- wecom_outbound_outbox.content becomes nullable: recommendation replies keep the
-- body only in the encrypted delivery envelope.
ALTER TABLE wecom_outbound_outbox MODIFY content MEDIUMTEXT NULL;

SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = @schema_name
          AND table_name = 'wecom_outbound_outbox'
          AND column_name = 'recommendation_delivery_id'
    ),
    'SELECT 1',
    'ALTER TABLE `wecom_outbound_outbox`
       ADD COLUMN `recommendation_delivery_id` CHAR(36) NULL,
       ADD UNIQUE KEY `uk_outbox_recommendation_delivery` (`recommendation_delivery_id`)'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = @schema_name
          AND table_name = 'conversation_log'
          AND column_name = 'recommendation_delivery_id'
    ),
    'SELECT 1',
    'ALTER TABLE `conversation_log`
       ADD COLUMN `recommendation_delivery_id` CHAR(36) NULL,
       ADD COLUMN `redaction_state` VARCHAR(24) NULL,
       ADD KEY `idx_conversation_recommendation_delivery` (`recommendation_delivery_id`)'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- Foreign keys follow the §9.11 contract.  attempt/delivery hold RESTRICT so the
-- TTL job must delete in the documented order, and the two nullable
-- back-references degrade to SET NULL.
SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_schema = @schema_name
          AND table_name = 'recommendation_search_attempt'
          AND constraint_name = 'fk_recommendation_attempt_request'
    ),
    'SELECT 1',
    'ALTER TABLE `recommendation_search_attempt`
       ADD CONSTRAINT `fk_recommendation_attempt_request`
       FOREIGN KEY (`request_id`) REFERENCES `recommendation_request`(`request_id`)
       ON DELETE RESTRICT'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_schema = @schema_name
          AND table_name = 'recommendation_delivery'
          AND constraint_name = 'fk_recommendation_delivery_request'
    ),
    'SELECT 1',
    'ALTER TABLE `recommendation_delivery`
       ADD CONSTRAINT `fk_recommendation_delivery_request`
       FOREIGN KEY (`request_id`) REFERENCES `recommendation_request`(`request_id`)
       ON DELETE RESTRICT'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_schema = @schema_name
          AND table_name = 'recommendation_request'
          AND constraint_name = 'fk_recommendation_request_parent'
    ),
    'SELECT 1',
    'ALTER TABLE `recommendation_request`
       ADD CONSTRAINT `fk_recommendation_request_parent`
       FOREIGN KEY (`parent_request_id`) REFERENCES `recommendation_request`(`request_id`)
       ON DELETE SET NULL'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- Circular reference: request.served_attempt_id can only be constrained after
-- recommendation_search_attempt exists.
SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_schema = @schema_name
          AND table_name = 'recommendation_request'
          AND constraint_name = 'fk_recommendation_request_served_attempt'
    ),
    'SELECT 1',
    'ALTER TABLE `recommendation_request`
       ADD CONSTRAINT `fk_recommendation_request_served_attempt`
       FOREIGN KEY (`served_attempt_id`) REFERENCES `recommendation_search_attempt`(`attempt_id`)
       ON DELETE SET NULL'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl = IF(
    EXISTS(
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_schema = @schema_name
          AND table_name = 'wecom_outbound_outbox'
          AND constraint_name = 'fk_outbox_recommendation_delivery'
    ),
    'SELECT 1',
    'ALTER TABLE `wecom_outbound_outbox`
       ADD CONSTRAINT `fk_outbox_recommendation_delivery`
       FOREIGN KEY (`recommendation_delivery_id`) REFERENCES `recommendation_delivery`(`delivery_id`)
       ON DELETE SET NULL'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
