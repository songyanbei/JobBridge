ALTER TABLE event_log
  ADD UNIQUE KEY uk_event_client_idempotency (userid, event_type, client_event_id);
