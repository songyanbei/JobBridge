-- Phase 14 rollback (run only after AIBot rollout is disabled and rows are audited).
-- Every index/column operation is guarded through information_schema so this
-- file is safe to run repeatedly and against a partially applied migration.

DROP PROCEDURE IF EXISTS phase14_drop_index_if_exists;
DROP PROCEDURE IF EXISTS phase14_drop_column_if_exists;
DELIMITER //
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
DELIMITER ;

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
