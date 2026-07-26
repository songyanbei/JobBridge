-- Incremental upgrade after phase9_002 was released.
ALTER TABLE recommendation_delivery
  ADD COLUMN content_expires_at DATETIME(6) NULL;
