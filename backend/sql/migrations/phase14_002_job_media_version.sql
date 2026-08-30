-- Phase 14 S4-5 additive media/version contract.
-- Safe for mixed fleets: old writers continue to insert NULL entity_version.
ALTER TABLE `media_asset_lifecycle`
  ADD COLUMN `entity_version` INT UNSIGNED NULL AFTER `entity_id`,
  ADD KEY `idx_media_entity_version`
    (`entity_type`,`entity_id`,`entity_version`,`state`);

-- Down migration (run only after the phase14 release gate has approved it):
-- ALTER TABLE `media_asset_lifecycle`
--   DROP KEY `idx_media_entity_version`, DROP COLUMN `entity_version`;
