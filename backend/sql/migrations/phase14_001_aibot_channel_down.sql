-- Phase 14 rollback (run only after AIBot rollout is disabled and rows are audited).
-- Existing inbound/outbox data is preserved; drop only additive metadata.
DROP TABLE IF EXISTS wecom_aibot_identity;
ALTER TABLE wecom_outbound_outbox
  DROP INDEX idx_outbox_channel_status_due,
  DROP INDEX idx_outbox_ordering_status;
ALTER TABLE wecom_inbound_event
  DROP INDEX uk_inbound_channel_provider,
  DROP INDEX uk_inbound_dedupe_key,
  DROP INDEX idx_inbound_ordering_status;

ALTER TABLE wecom_outbound_outbox
  DROP COLUMN channel, DROP COLUMN conversation_type, DROP COLUMN conversation_id,
  DROP COLUMN chat_id, DROP COLUMN ordering_key, DROP COLUMN provider_req_id,
  DROP COLUMN reply_command, DROP COLUMN stream_id, DROP COLUMN finish,
  DROP COLUMN provider_response, DROP COLUMN reply_expires_at,
  DROP COLUMN stream_deadline_at, DROP COLUMN ack_req_id, DROP COLUMN ack_received_at,
  DROP COLUMN first_sent_at, DROP COLUMN uncertain_at, DROP COLUMN provider_close_code,
  DROP COLUMN lease_owner, DROP COLUMN fencing_token;
ALTER TABLE wecom_inbound_event
  DROP COLUMN source_channel, DROP COLUMN provider_msg_id, DROP COLUMN dedupe_key,
  DROP COLUMN conversation_type, DROP COLUMN conversation_id, DROP COLUMN chat_id,
  DROP COLUMN ordering_key, DROP COLUMN provider_req_id, DROP COLUMN aibot_id,
  DROP COLUMN actor_id_kind;
