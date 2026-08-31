-- Phase 14 P1: AIBot durable inbox/outbox channel metadata.
--
-- This migration is intentionally additive and safe to re-run.  Every ALTER
-- is guarded by information_schema so an interrupted deployment can resume.
-- Rollback: stop AIBot acceptance/writers, verify no new AIBot rows are being
-- written, then execute the guarded DOWN section at the end.  The DOWN section
-- removes only Phase 14 columns/constraints; it never deletes inbound/outbox
-- data.  Export AIBot rows before rollback if they must be retained.

SET @schema_name = DATABASE();

-- Add one column at a time because MySQL has no portable IF NOT EXISTS for
-- ALTER TABLE ADD COLUMN across the supported 8.0 minor versions.
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_inbound_event' AND column_name='turn_id')=0,
  'ALTER TABLE wecom_inbound_event ADD COLUMN turn_id VARCHAR(36) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_inbound_event' AND column_name='source_channel')=0,
  'ALTER TABLE wecom_inbound_event ADD COLUMN source_channel VARCHAR(24) NOT NULL DEFAULT ''wecom_app''', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_inbound_event' AND column_name='provider_msg_id')=0,
  'ALTER TABLE wecom_inbound_event ADD COLUMN provider_msg_id VARCHAR(128) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_inbound_event' AND column_name='dedupe_key')=0,
  'ALTER TABLE wecom_inbound_event ADD COLUMN dedupe_key CHAR(64) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_inbound_event' AND column_name='conversation_type')=0,
  'ALTER TABLE wecom_inbound_event ADD COLUMN conversation_type VARCHAR(16) NOT NULL DEFAULT ''single''', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_inbound_event' AND column_name='conversation_id')=0,
  'ALTER TABLE wecom_inbound_event ADD COLUMN conversation_id VARCHAR(128) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_inbound_event' AND column_name='chat_id')=0,
  'ALTER TABLE wecom_inbound_event ADD COLUMN chat_id VARCHAR(128) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_inbound_event' AND column_name='ordering_key')=0,
  'ALTER TABLE wecom_inbound_event ADD COLUMN ordering_key VARCHAR(192) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_inbound_event' AND column_name='provider_req_id')=0,
  'ALTER TABLE wecom_inbound_event ADD COLUMN provider_req_id VARCHAR(128) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_inbound_event' AND column_name='aibot_id')=0,
  'ALTER TABLE wecom_inbound_event ADD COLUMN aibot_id VARCHAR(128) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_inbound_event' AND column_name='actor_id_kind')=0,
  'ALTER TABLE wecom_inbound_event ADD COLUMN actor_id_kind VARCHAR(16) NOT NULL DEFAULT ''plain''', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_inbound_event' AND column_name='media_url_ciphertext')=0,
  'ALTER TABLE wecom_inbound_event ADD COLUMN media_url_ciphertext MEDIUMBLOB NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_inbound_event' AND column_name='media_aes_key_ciphertext')=0,
  'ALTER TABLE wecom_inbound_event ADD COLUMN media_aes_key_ciphertext VARBINARY(512) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_inbound_event' AND column_name='media_expires_at')=0,
  'ALTER TABLE wecom_inbound_event ADD COLUMN media_expires_at DATETIME(6) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_inbound_event' AND column_name='media_storage_ref')=0,
  'ALTER TABLE wecom_inbound_event ADD COLUMN media_storage_ref VARCHAR(512) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_inbound_event' AND column_name='media_download_status')=0,
  'ALTER TABLE wecom_inbound_event ADD COLUMN media_download_status VARCHAR(24) NOT NULL DEFAULT ''pending''', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_inbound_event' AND column_name='media_download_attempts')=0,
  'ALTER TABLE wecom_inbound_event ADD COLUMN media_download_attempts INT UNSIGNED NOT NULL DEFAULT 0', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_outbound_outbox' AND column_name='channel')=0,
  'ALTER TABLE wecom_outbound_outbox ADD COLUMN channel VARCHAR(24) NOT NULL DEFAULT ''wecom_app''', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_outbound_outbox' AND column_name='conversation_type')=0,
  'ALTER TABLE wecom_outbound_outbox ADD COLUMN conversation_type VARCHAR(16) NOT NULL DEFAULT ''single''', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_outbound_outbox' AND column_name='conversation_id')=0,
  'ALTER TABLE wecom_outbound_outbox ADD COLUMN conversation_id VARCHAR(128) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_outbound_outbox' AND column_name='chat_id')=0,
  'ALTER TABLE wecom_outbound_outbox ADD COLUMN chat_id VARCHAR(128) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_outbound_outbox' AND column_name='ordering_key')=0,
  'ALTER TABLE wecom_outbound_outbox ADD COLUMN ordering_key VARCHAR(192) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_outbound_outbox' AND column_name='provider_req_id')=0,
  'ALTER TABLE wecom_outbound_outbox ADD COLUMN provider_req_id VARCHAR(128) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_outbound_outbox' AND column_name='reply_command')=0,
  'ALTER TABLE wecom_outbound_outbox ADD COLUMN reply_command VARCHAR(40) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_outbound_outbox' AND column_name='stream_id')=0,
  'ALTER TABLE wecom_outbound_outbox ADD COLUMN stream_id VARCHAR(128) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_outbound_outbox' AND column_name='finish')=0,
  'ALTER TABLE wecom_outbound_outbox ADD COLUMN finish TINYINT(1) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_outbound_outbox' AND column_name='provider_response')=0,
  'ALTER TABLE wecom_outbound_outbox ADD COLUMN provider_response JSON NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_outbound_outbox' AND column_name='reply_expires_at')=0,
  'ALTER TABLE wecom_outbound_outbox ADD COLUMN reply_expires_at DATETIME(6) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_outbound_outbox' AND column_name='stream_deadline_at')=0,
  'ALTER TABLE wecom_outbound_outbox ADD COLUMN stream_deadline_at DATETIME(6) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_outbound_outbox' AND column_name='ack_req_id')=0,
  'ALTER TABLE wecom_outbound_outbox ADD COLUMN ack_req_id VARCHAR(128) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_outbound_outbox' AND column_name='ack_received_at')=0,
  'ALTER TABLE wecom_outbound_outbox ADD COLUMN ack_received_at DATETIME(6) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_outbound_outbox' AND column_name='first_sent_at')=0,
  'ALTER TABLE wecom_outbound_outbox ADD COLUMN first_sent_at DATETIME(6) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_outbound_outbox' AND column_name='uncertain_at')=0,
  'ALTER TABLE wecom_outbound_outbox ADD COLUMN uncertain_at DATETIME(6) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_outbound_outbox' AND column_name='provider_close_code')=0,
  'ALTER TABLE wecom_outbound_outbox ADD COLUMN provider_close_code VARCHAR(32) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_outbound_outbox' AND column_name='lease_owner')=0,
  'ALTER TABLE wecom_outbound_outbox ADD COLUMN lease_owner VARCHAR(64) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_outbound_outbox' AND column_name='fencing_token')=0,
  'ALTER TABLE wecom_outbound_outbox ADD COLUMN fencing_token BIGINT UNSIGNED NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- Existing rows are legacy single conversations.  Populate deterministic
-- ordering/session values while leaving provider identity NULL.
UPDATE wecom_inbound_event
   SET source_channel=COALESCE(source_channel,'wecom_app'),
       conversation_type=COALESCE(conversation_type,'single'),
       conversation_id=COALESCE(conversation_id, from_userid),
       ordering_key=COALESCE(ordering_key, CONCAT('wecom:', COALESCE(source_channel,'wecom_app'), ':single:', COALESCE(from_userid,'')))
 WHERE ordering_key IS NULL OR conversation_id IS NULL;
UPDATE wecom_outbound_outbox o
   LEFT JOIN wecom_inbound_event i ON i.id=o.inbound_event_id
   SET o.channel=COALESCE(o.channel,'wecom_app'),
       o.conversation_type=COALESCE(o.conversation_type,'single'),
       o.conversation_id=COALESCE(o.conversation_id, i.conversation_id, o.userid),
       o.ordering_key=COALESCE(o.ordering_key, i.ordering_key, CONCAT('wecom:', COALESCE(o.channel,'wecom_app'), ':single:', COALESCE(o.userid,'')))
 WHERE o.ordering_key IS NULL OR o.conversation_id IS NULL;

-- Status is the sole outbox state machine; retain all existing values.
ALTER TABLE wecom_outbound_outbox MODIFY status ENUM('pending','sending','sent','uncertain','dead_letter') NOT NULL DEFAULT 'pending';

-- Add indexes/unique keys only when absent.  128/192-byte key parts remain
-- below MySQL 8 utf8mb4 index limits when combined with the declared lengths.
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema=@schema_name AND table_name='wecom_inbound_event' AND index_name='uk_inbound_channel_provider')=0,
  'ALTER TABLE wecom_inbound_event ADD UNIQUE KEY uk_inbound_channel_provider (source_channel,provider_msg_id)', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema=@schema_name AND table_name='wecom_inbound_event' AND index_name='uk_inbound_dedupe_key')=0,
  'ALTER TABLE wecom_inbound_event ADD UNIQUE KEY uk_inbound_dedupe_key (dedupe_key)', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema=@schema_name AND table_name='wecom_inbound_event' AND index_name='idx_inbound_ordering_status')=0,
  'ALTER TABLE wecom_inbound_event ADD KEY idx_inbound_ordering_status (ordering_key,status,id)', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema=@schema_name AND table_name='wecom_outbound_outbox' AND index_name='idx_outbox_channel_status_due')=0,
  'ALTER TABLE wecom_outbound_outbox ADD KEY idx_outbox_channel_status_due (channel,status,next_attempt_at,id)', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema=@schema_name AND table_name='wecom_outbound_outbox' AND index_name='idx_outbox_ordering_status')=0,
  'ALTER TABLE wecom_outbound_outbox ADD KEY idx_outbox_ordering_status (ordering_key,status,id)', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- New identity mapping deliberately stores no secret or decrypt material.
CREATE TABLE IF NOT EXISTS wecom_aibot_identity (
  opaque_actor_id VARCHAR(128) NOT NULL,
  mapped_external_userid VARCHAR(64) NULL,
  identity_status ENUM('unverified','verified','rejected') NOT NULL DEFAULT 'unverified',
  verified_at DATETIME(6) NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (opaque_actor_id), KEY idx_aibot_identity_mapped (mapped_external_userid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- CHECK constraints are guarded because MySQL 8.0.16+ enforces them.
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.table_constraints WHERE constraint_schema=@schema_name AND table_name='wecom_inbound_event' AND constraint_name='ck_inbound_channel_contract')=0,
  'ALTER TABLE wecom_inbound_event ADD CONSTRAINT ck_inbound_channel_contract CHECK ((source_channel = ''wecom_app'') OR (source_channel = ''wecom_aibot'' AND provider_msg_id IS NOT NULL AND CHAR_LENGTH(provider_msg_id) BETWEEN 1 AND 128 AND dedupe_key IS NOT NULL))', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.table_constraints WHERE constraint_schema=@schema_name AND table_name='wecom_inbound_event' AND constraint_name='ck_inbound_conversation_contract')=0,
  'ALTER TABLE wecom_inbound_event ADD CONSTRAINT ck_inbound_conversation_contract CHECK ((conversation_type = ''single'' AND conversation_id IS NOT NULL AND (source_channel = ''wecom_aibot'' OR from_userid IS NULL OR conversation_id = from_userid)) OR (conversation_type = ''group'' AND chat_id IS NOT NULL AND conversation_id = chat_id AND ordering_key IS NOT NULL))', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.table_constraints WHERE constraint_schema=@schema_name AND table_name='wecom_outbound_outbox' AND constraint_name='ck_outbox_conversation_contract')=0,
  'ALTER TABLE wecom_outbound_outbox ADD CONSTRAINT ck_outbox_conversation_contract CHECK ((conversation_type = ''single'' AND conversation_id IS NOT NULL AND (channel = ''wecom_aibot'' OR userid IS NULL OR conversation_id = userid)) OR (conversation_type = ''group'' AND chat_id IS NOT NULL AND conversation_id = chat_id AND ordering_key IS NOT NULL AND userid IS NULL))', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- ------------------------------ DOWN (manual, guarded) -------------------
-- Run only after AIBot is disabled and rows are exported:
-- ALTER TABLE wecom_inbound_event DROP CHECK ck_inbound_channel_contract, DROP CHECK ck_inbound_conversation_contract;
-- ALTER TABLE wecom_outbound_outbox DROP CHECK ck_outbox_conversation_contract;
-- ALTER TABLE wecom_inbound_event DROP INDEX uk_inbound_channel_provider, DROP INDEX uk_inbound_dedupe_key, DROP INDEX idx_inbound_ordering_status;
-- ALTER TABLE wecom_outbound_outbox DROP INDEX idx_outbox_channel_status_due, DROP INDEX idx_outbox_ordering_status;
-- ALTER TABLE wecom_outbound_outbox MODIFY status ENUM('pending','sending','sent','dead_letter') NOT NULL DEFAULT 'pending';
-- DROP TABLE wecom_aibot_identity;
-- Then issue guarded DROP COLUMN statements for the Phase 14 columns above.
