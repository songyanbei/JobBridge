-- Backfill the TTL column added by phase9_005 without rewriting that published
-- migration. Existing shorter deadlines are preserved.

UPDATE `recommendation_delivery`
SET `content_expires_at` = CASE
  WHEN `status` IN ('sent', 'permanent_failed')
    THEN DATE_ADD(
      COALESCE(`sent_at`, `updated_at`, `created_at`),
      INTERVAL 24 HOUR
    )
  WHEN `status` = 'prepared'
    THEN DATE_ADD(`created_at`, INTERVAL 24 HOUR)
  WHEN `status` IN ('pending', 'sending', 'retry_wait', 'unknown')
    THEN DATE_ADD(`created_at`, INTERVAL 7 DAY)
  ELSE DATE_ADD(
    COALESCE(`updated_at`, `created_at`),
    INTERVAL 24 HOUR
  )
END
WHERE `content_expires_at` IS NULL;
