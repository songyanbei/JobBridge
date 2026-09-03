-- Phase 17 guarded down migration.
-- Stop demo traffic, export the control-plane audit evidence, and verify that
-- all demo resources have reached cleaned before executing this file manually.
-- This file only drops the additive demo control-plane tables.

DROP TABLE IF EXISTS `demo_resource`;
DROP TABLE IF EXISTS `demo_principal`;
DROP TABLE IF EXISTS `demo_workspace_member`;
DROP TABLE IF EXISTS `demo_workspace`;
