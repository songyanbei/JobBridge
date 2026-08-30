-- Phase 14-002 down migration.  Execute only after the release gate confirms
-- no writer/consumer reads entity_version.
ALTER TABLE `media_asset_lifecycle`
  DROP KEY `idx_media_entity_version`,
  DROP COLUMN `entity_version`;
