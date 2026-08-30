-- Additive Contact binding for bidirectional recruitment flows.
ALTER TABLE `contact_request` ADD COLUMN `direction` VARCHAR(32) NULL;
ALTER TABLE `contact_grant` ADD COLUMN `direction` VARCHAR(32) NULL;
CREATE INDEX `idx_contact_request_direction` ON `contact_request` (`direction`, `listing_ref`, `status`);
