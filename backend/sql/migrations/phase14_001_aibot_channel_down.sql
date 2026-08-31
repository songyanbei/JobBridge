-- Phase 14 rollback (run only after AIBot rollout is disabled and rows are audited).
-- Every index/column operation is guarded through information_schema so this
-- file is safe to run repeatedly and against a partially applied migration.

DROP PROCEDURE IF EXISTS phase14_assert_aibot_rollback_guards;
DROP PROCEDURE IF EXISTS phase14_drop_index_if_exists;
DROP PROCEDURE IF EXISTS phase14_drop_column_if_exists;
DROP PROCEDURE IF EXISTS phase14_drop_check_if_exists;
DELIMITER //
CREATE PROCEDURE phase14_assert_aibot_rollback_guards()
BEGIN
    DECLARE v_rollout_enabled VARCHAR(32) DEFAULT NULL;
    DECLARE v_inbound BIGINT DEFAULT 0;
    DECLARE v_outbox BIGINT DEFAULT 0;
    DECLARE v_identity BIGINT DEFAULT 0;
    DECLARE v_audit BIGINT DEFAULT 0;

    SELECT config_value INTO v_rollout_enabled
      FROM system_config
     WHERE config_key = 'wecom_aibot.rollout_enabled'
     LIMIT 1;
    IF v_rollout_enabled IS NULL
       OR LOWER(TRIM(v_rollout_enabled)) NOT IN ('0', 'false', 'off', 'disabled') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'phase14 rollback blocked: aibot rollout is not disabled';
    END IF;

    SELECT COUNT(*) INTO v_inbound
      FROM wecom_inbound_event
     WHERE source_channel = 'wecom_aibot';
    SELECT COUNT(*) INTO v_outbox
      FROM wecom_outbound_outbox
     WHERE channel = 'wecom_aibot';
    SELECT COUNT(*) INTO v_identity
      FROM wecom_aibot_identity;
    IF v_inbound <> 0 OR v_outbox <> 0 OR v_identity <> 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'phase14 rollback blocked: aibot data cleanup is incomplete';
    END IF;

    SELECT COUNT(*) INTO v_audit
      FROM audit_log
     WHERE target_type = 'system'
       AND target_id = 'wecom_aibot'
       AND action = 'strategy_rollback'
       AND operator IS NOT NULL AND TRIM(operator) <> ''
       AND reason LIKE '%phase14%';
    IF v_audit = 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'phase14 rollback blocked: audit confirmation is missing';
    END IF;
END//

CREATE PROCEDURE phase14_drop_index_if_exists(
    IN p_table VARCHAR(64), IN p_index VARCHAR(64)
)
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.statistics
        WHERE table_schema = DATABASE()
          AND table_name = p_table
          AND index_name = p_index
    ) THEN
        SET @phase14_sql = CONCAT(
            'ALTER TABLE `', REPLACE(DATABASE(), '`', '``'), '`.',
            '`', REPLACE(p_table, '`', '``'), '` DROP INDEX `',
            REPLACE(p_index, '`', '``'), '`'
        );
        PREPARE phase14_stmt FROM @phase14_sql;
        EXECUTE phase14_stmt;
        DEALLOCATE PREPARE phase14_stmt;
    END IF;
END//

CREATE PROCEDURE phase14_drop_column_if_exists(
    IN p_table VARCHAR(64), IN p_column VARCHAR(64)
)
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = p_table
          AND column_name = p_column
    ) THEN
        SET @phase14_sql = CONCAT(
            'ALTER TABLE `', REPLACE(DATABASE(), '`', '``'), '`.',
            '`', REPLACE(p_table, '`', '``'), '` DROP COLUMN `',
            REPLACE(p_column, '`', '``'), '`'
        );
        PREPARE phase14_stmt FROM @phase14_sql;
        EXECUTE phase14_stmt;
        DEALLOCATE PREPARE phase14_stmt;
    END IF;
END//

CREATE PROCEDURE phase14_drop_check_if_exists(
    IN p_table VARCHAR(64), IN p_constraint VARCHAR(64)
)
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_schema = DATABASE()
          AND table_name = p_table
          AND constraint_name = p_constraint
          AND constraint_type = 'CHECK'
    ) THEN
        SET @phase14_sql = CONCAT(
            'ALTER TABLE `', REPLACE(DATABASE(), '`', '``'), '`.',
            '`', REPLACE(p_table, '`', '``'), '` DROP CHECK `',
            REPLACE(p_constraint, '`', '``'), '`'
        );
        PREPARE phase14_stmt FROM @phase14_sql;
        EXECUTE phase14_stmt;
        DEALLOCATE PREPARE phase14_stmt;
    END IF;
END//
DELIMITER ;

CALL phase14_assert_aibot_rollback_guards();

-- Constraints must be removed before their referenced columns.
CALL phase14_drop_check_if_exists('wecom_inbound_event', 'ck_inbound_channel_contract');
CALL phase14_drop_check_if_exists('wecom_inbound_event', 'ck_inbound_conversation_contract');
CALL phase14_drop_check_if_exists('wecom_outbound_outbox', 'ck_outbox_conversation_contract');

CALL phase14_drop_index_if_exists('wecom_outbound_outbox', 'idx_outbox_channel_status_due');
CALL phase14_drop_index_if_exists('wecom_outbound_outbox', 'idx_outbox_ordering_status');
CALL phase14_drop_index_if_exists('wecom_inbound_event', 'uk_inbound_channel_provider');
CALL phase14_drop_index_if_exists('wecom_inbound_event', 'uk_inbound_dedupe_key');
CALL phase14_drop_index_if_exists('wecom_inbound_event', 'idx_inbound_ordering_status');

CALL phase14_drop_column_if_exists('wecom_outbound_outbox', 'channel');
CALL phase14_drop_column_if_exists('wecom_outbound_outbox', 'conversation_type');
CALL phase14_drop_column_if_exists('wecom_outbound_outbox', 'conversation_id');
CALL phase14_drop_column_if_exists('wecom_outbound_outbox', 'chat_id');
CALL phase14_drop_column_if_exists('wecom_outbound_outbox', 'ordering_key');
CALL phase14_drop_column_if_exists('wecom_outbound_outbox', 'provider_req_id');
CALL phase14_drop_column_if_exists('wecom_outbound_outbox', 'reply_command');
CALL phase14_drop_column_if_exists('wecom_outbound_outbox', 'stream_id');
CALL phase14_drop_column_if_exists('wecom_outbound_outbox', 'finish');
CALL phase14_drop_column_if_exists('wecom_outbound_outbox', 'provider_response');
CALL phase14_drop_column_if_exists('wecom_outbound_outbox', 'reply_expires_at');
CALL phase14_drop_column_if_exists('wecom_outbound_outbox', 'stream_deadline_at');
CALL phase14_drop_column_if_exists('wecom_outbound_outbox', 'ack_req_id');
CALL phase14_drop_column_if_exists('wecom_outbound_outbox', 'ack_received_at');
CALL phase14_drop_column_if_exists('wecom_outbound_outbox', 'first_sent_at');
CALL phase14_drop_column_if_exists('wecom_outbound_outbox', 'uncertain_at');
CALL phase14_drop_column_if_exists('wecom_outbound_outbox', 'provider_close_code');
CALL phase14_drop_column_if_exists('wecom_outbound_outbox', 'lease_owner');
CALL phase14_drop_column_if_exists('wecom_outbound_outbox', 'fencing_token');

CALL phase14_drop_column_if_exists('wecom_inbound_event', 'source_channel');
CALL phase14_drop_column_if_exists('wecom_inbound_event', 'provider_msg_id');
CALL phase14_drop_column_if_exists('wecom_inbound_event', 'dedupe_key');
CALL phase14_drop_column_if_exists('wecom_inbound_event', 'conversation_type');
CALL phase14_drop_column_if_exists('wecom_inbound_event', 'conversation_id');
CALL phase14_drop_column_if_exists('wecom_inbound_event', 'chat_id');
CALL phase14_drop_column_if_exists('wecom_inbound_event', 'ordering_key');
CALL phase14_drop_column_if_exists('wecom_inbound_event', 'provider_req_id');
CALL phase14_drop_column_if_exists('wecom_inbound_event', 'aibot_id');
CALL phase14_drop_column_if_exists('wecom_inbound_event', 'actor_id_kind');
CALL phase14_drop_column_if_exists('wecom_inbound_event', 'media_url_ciphertext');
CALL phase14_drop_column_if_exists('wecom_inbound_event', 'media_aes_key_ciphertext');
CALL phase14_drop_column_if_exists('wecom_inbound_event', 'media_expires_at');
CALL phase14_drop_column_if_exists('wecom_inbound_event', 'media_storage_ref');
CALL phase14_drop_column_if_exists('wecom_inbound_event', 'media_download_status');
CALL phase14_drop_column_if_exists('wecom_inbound_event', 'media_download_attempts');

-- DROP TABLE IF EXISTS is itself repeat-safe; identity rows are additive only.
DROP TABLE IF EXISTS wecom_aibot_identity;

DROP PROCEDURE IF EXISTS phase14_drop_index_if_exists;
DROP PROCEDURE IF EXISTS phase14_drop_column_if_exists;
DROP PROCEDURE IF EXISTS phase14_drop_check_if_exists;
DROP PROCEDURE IF EXISTS phase14_assert_aibot_rollback_guards;
