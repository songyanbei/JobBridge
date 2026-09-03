-- Phase 17 guarded down migration.
-- Stop demo traffic, export the control-plane audit evidence, and verify that
-- all demo resources have reached cleaned before executing this file manually.
-- This file only drops the additive demo control-plane tables.

DROP PROCEDURE IF EXISTS `phase17_assert_demo_down_guards`;
DELIMITER //
CREATE PROCEDURE `phase17_assert_demo_down_guards`()
BEGIN
  IF EXISTS (
    SELECT 1 FROM `demo_workspace` WHERE `status` <> 'cleaned' LIMIT 1
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'phase17 down blocked: workspace is not cleaned';
  END IF;

  IF EXISTS (
    SELECT 1 FROM `demo_workspace_member`
      WHERE `membership_status` = 'active' LIMIT 1
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'phase17 down blocked: active demo member exists';
  END IF;

  IF EXISTS (
    SELECT 1 FROM `demo_resource`
      WHERE `lifecycle_status` <> 'cleaned' LIMIT 1
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'phase17 down blocked: demo resource is not cleaned';
  END IF;

  IF EXISTS (
    SELECT 1 FROM `wecom_outbound_outbox`
      WHERE `demo_id` IS NOT NULL
        AND `channel` = 'wecom_aibot'
        AND `status` IN ('pending', 'sending')
      LIMIT 1
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'phase17 down blocked: demo AIBot outbox is pending';
  END IF;

  IF EXISTS (
    SELECT 1 FROM `wecom_inbound_event`
      WHERE `demo_id` IS NOT NULL
        AND `status` IN ('received', 'processing', 'session_pending')
      LIMIT 1
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'phase17 down blocked: demo inbound event is in flight';
  END IF;
END//
DELIMITER ;

CALL `phase17_assert_demo_down_guards`();
DROP PROCEDURE IF EXISTS `phase17_assert_demo_down_guards`;

DROP TABLE IF EXISTS `demo_resource`;
DROP TABLE IF EXISTS `demo_principal`;
DROP TABLE IF EXISTS `demo_workspace_member`;
DROP TABLE IF EXISTS `demo_workspace`;
