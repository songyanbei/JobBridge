-- Add the terminal state used after durable media deletion exhausts retries.
ALTER TABLE `media_asset_lifecycle`
  MODIFY COLUMN `state`
    ENUM('pending','attached','delete_pending','deleted','dead_letter')
    NOT NULL DEFAULT 'pending';

-- Existing rows that already crossed the new threshold must stop retrying.
UPDATE `media_asset_lifecycle`
SET `state` = 'dead_letter',
    `next_attempt_at` = NULL,
    `lease_owner` = NULL,
    `lease_expires_at` = NULL,
    `last_error` = COALESCE(`last_error`, 'media deletion retry limit reached')
WHERE `state` = 'delete_pending'
  AND `attempt_count` >= 10;
