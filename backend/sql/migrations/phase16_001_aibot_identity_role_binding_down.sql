-- Phase 16 guarded rollback.
-- Run only after AIBot acceptance, identity resolution and role binding are
-- disabled, exports are verified, and an operator has recorded confirmation.
-- This script never deletes User rows or legacy channel data.

DROP PROCEDURE IF EXISTS phase16_assert_aibot_rollback_guards;
DROP PROCEDURE IF EXISTS phase16_drop_fk_if_exists;
DROP PROCEDURE IF EXISTS phase16_drop_index_if_exists;
DROP PROCEDURE IF EXISTS phase16_drop_column_if_exists;
DELIMITER //

CREATE PROCEDURE phase16_assert_aibot_rollback_guards()
rollback_guard: BEGIN
    DECLARE v_tables INT DEFAULT 0;
    DECLARE v_phase16_columns INT DEFAULT 0;
    DECLARE v_aibot_enabled VARCHAR(32) DEFAULT NULL;
    DECLARE v_identity_enabled VARCHAR(32) DEFAULT NULL;
    DECLARE v_binding BIGINT DEFAULT 0;
    DECLARE v_registration BIGINT DEFAULT 0;
    DECLARE v_invite BIGINT DEFAULT 0;
    DECLARE v_identity_audit BIGINT DEFAULT 0;
    DECLARE v_confirmations BIGINT DEFAULT 0;

    SELECT COUNT(*) INTO v_tables
      FROM information_schema.tables
     WHERE table_schema = DATABASE()
       AND table_name IN ('aibot_identity_binding', 'aibot_registration',
                          'aibot_role_invite', 'aibot_identity_audit',
                          'wecom_aibot_identity');
    SELECT COUNT(*) INTO v_phase16_columns
      FROM information_schema.columns
     WHERE table_schema = DATABASE()
       AND table_name = 'wecom_aibot_identity'
       AND column_name IN ('bot_id', 'actor_id_kind', 'opaque_actor_digest',
                           'canonical_userid', 'resolution_attempts');

    -- A completed rollback is a safe no-op.  Any partially applied state is
    -- blocked rather than guessed at.
    IF v_phase16_columns = 0 AND v_tables <= 1 THEN
        LEAVE rollback_guard;
    END IF;
    IF v_tables <> 5 OR v_phase16_columns < 5 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'phase16 rollback blocked: migration is incomplete';
    END IF;

    SELECT config_value INTO v_aibot_enabled
      FROM system_config WHERE config_key = 'wecom_aibot.rollout_enabled' LIMIT 1;
    SELECT config_value INTO v_identity_enabled
      FROM system_config WHERE config_key = 'aibot.identity_resolution_enabled' LIMIT 1;
    IF v_aibot_enabled IS NULL
       OR LOWER(TRIM(v_aibot_enabled)) NOT IN ('0', 'false', 'off', 'disabled') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'phase16 rollback blocked: aibot acceptance is not disabled';
    END IF;
    IF v_identity_enabled IS NULL
       OR LOWER(TRIM(v_identity_enabled)) NOT IN ('0', 'false', 'off', 'disabled') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'phase16 rollback blocked: identity resolution is not disabled';
    END IF;

    SELECT COUNT(*) INTO v_binding FROM aibot_identity_binding;
    SELECT COUNT(*) INTO v_registration FROM aibot_registration;
    SELECT COUNT(*) INTO v_invite FROM aibot_role_invite;
    SELECT COUNT(*) INTO v_identity_audit FROM aibot_identity_audit;
    SELECT COUNT(*) INTO v_confirmations
      FROM audit_log
     WHERE target_type = 'system'
       AND target_id = 'wecom_aibot'
       AND action = 'phase16_down_confirmation'
       AND operator IS NOT NULL AND TRIM(operator) <> ''
       AND reason LIKE '%export_confirmed%'
       AND reason LIKE '%cleanup_confirmed%'
       AND reason LIKE '%audit_confirmed%';
    IF v_binding <> 0 OR v_registration <> 0 OR v_invite <> 0
       OR v_identity_audit <> 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'phase16 rollback blocked: identity data export/cleanup is incomplete';
    END IF;
    IF v_confirmations = 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'phase16 rollback blocked: export, cleanup and audit confirmation is missing';
    END IF;
END//

CREATE PROCEDURE phase16_drop_fk_if_exists(IN p_table VARCHAR(64), IN p_fk VARCHAR(64))
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.table_constraints
                WHERE constraint_schema=DATABASE() AND table_name=p_table
                  AND constraint_name=p_fk AND constraint_type='FOREIGN KEY') THEN
        SET @phase16_sql = CONCAT('ALTER TABLE `', REPLACE(p_table,'`','``'), '` DROP FOREIGN KEY `', REPLACE(p_fk,'`','``'), '`');
        PREPARE phase16_stmt FROM @phase16_sql; EXECUTE phase16_stmt; DEALLOCATE PREPARE phase16_stmt;
    END IF;
END//

CREATE PROCEDURE phase16_drop_index_if_exists(IN p_table VARCHAR(64), IN p_index VARCHAR(64))
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.statistics
                WHERE table_schema=DATABASE() AND table_name=p_table AND index_name=p_index) THEN
        SET @phase16_sql = CONCAT('ALTER TABLE `', REPLACE(p_table,'`','``'), '` DROP INDEX `', REPLACE(p_index,'`','``'), '`');
        PREPARE phase16_stmt FROM @phase16_sql; EXECUTE phase16_stmt; DEALLOCATE PREPARE phase16_stmt;
    END IF;
END//

CREATE PROCEDURE phase16_drop_column_if_exists(IN p_table VARCHAR(64), IN p_column VARCHAR(64))
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_schema=DATABASE() AND table_name=p_table AND column_name=p_column) THEN
        SET @phase16_sql = CONCAT('ALTER TABLE `', REPLACE(p_table,'`','``'), '` DROP COLUMN `', REPLACE(p_column,'`','``'), '`');
        PREPARE phase16_stmt FROM @phase16_sql; EXECUTE phase16_stmt; DEALLOCATE PREPARE phase16_stmt;
    END IF;
END//
DELIMITER ;

CALL phase16_assert_aibot_rollback_guards();

-- Remove dependent objects before dropping their parent tables/columns.
CALL phase16_drop_fk_if_exists('aibot_identity_binding', 'fk_aibot_binding_user');
DROP TABLE IF EXISTS aibot_registration;
DROP TABLE IF EXISTS aibot_identity_binding;
DROP TABLE IF EXISTS aibot_role_invite;
DROP TABLE IF EXISTS aibot_identity_audit;

CALL phase16_drop_index_if_exists('wecom_aibot_identity', 'uk_aibot_identity_bot_digest');
CALL phase16_drop_index_if_exists('wecom_aibot_identity', 'uk_aibot_identity_bot_canonical');
CALL phase16_drop_index_if_exists('wecom_aibot_identity', 'idx_aibot_identity_status_due');
CALL phase16_drop_index_if_exists('wecom_aibot_identity', 'idx_aibot_identity_canonical');

CALL phase16_drop_column_if_exists('wecom_aibot_identity', 'revoked_at');
CALL phase16_drop_column_if_exists('wecom_aibot_identity', 'last_seen_at');
CALL phase16_drop_column_if_exists('wecom_aibot_identity', 'first_seen_at');
CALL phase16_drop_column_if_exists('wecom_aibot_identity', 'source_msg_id');
CALL phase16_drop_column_if_exists('wecom_aibot_identity', 'last_error_digest');
CALL phase16_drop_column_if_exists('wecom_aibot_identity', 'last_error_code');
CALL phase16_drop_column_if_exists('wecom_aibot_identity', 'next_resolution_at');
CALL phase16_drop_column_if_exists('wecom_aibot_identity', 'resolution_attempts');
CALL phase16_drop_column_if_exists('wecom_aibot_identity', 'canonical_userid');
CALL phase16_drop_column_if_exists('wecom_aibot_identity', 'opaque_actor_digest');
CALL phase16_drop_column_if_exists('wecom_aibot_identity', 'actor_id_kind');
CALL phase16_drop_column_if_exists('wecom_aibot_identity', 'bot_id');

DROP PROCEDURE IF EXISTS phase16_drop_fk_if_exists;
DROP PROCEDURE IF EXISTS phase16_drop_index_if_exists;
DROP PROCEDURE IF EXISTS phase16_drop_column_if_exists;
DROP PROCEDURE IF EXISTS phase16_assert_aibot_rollback_guards;
