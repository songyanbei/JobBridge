-- Phase 16: AIBot identity resolution and explicit role binding.
-- Additive/idempotent migration.  Stop AIBot acceptance before any down work.
SET @schema_name = DATABASE();

-- Extend the Phase 14 identity table without changing its opaque primary key.
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_aibot_identity' AND column_name='bot_id')=0,
  'ALTER TABLE wecom_aibot_identity ADD COLUMN bot_id VARCHAR(128) NOT NULL DEFAULT ''''', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_aibot_identity' AND column_name='actor_id_kind')=0,
  'ALTER TABLE wecom_aibot_identity ADD COLUMN actor_id_kind ENUM(''plain'',''open_userid'') NOT NULL DEFAULT ''open_userid''', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_aibot_identity' AND column_name='opaque_actor_digest')=0,
  'ALTER TABLE wecom_aibot_identity ADD COLUMN opaque_actor_digest CHAR(64) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_aibot_identity' AND column_name='canonical_userid')=0,
  'ALTER TABLE wecom_aibot_identity ADD COLUMN canonical_userid VARCHAR(64) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_aibot_identity' AND column_name='resolution_attempts')=0,
  'ALTER TABLE wecom_aibot_identity ADD COLUMN resolution_attempts INT UNSIGNED NOT NULL DEFAULT 0', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_aibot_identity' AND column_name='next_resolution_at')=0,
  'ALTER TABLE wecom_aibot_identity ADD COLUMN next_resolution_at DATETIME(6) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_aibot_identity' AND column_name='last_error_code')=0,
  'ALTER TABLE wecom_aibot_identity ADD COLUMN last_error_code VARCHAR(64) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_aibot_identity' AND column_name='last_error_digest')=0,
  'ALTER TABLE wecom_aibot_identity ADD COLUMN last_error_digest CHAR(64) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_aibot_identity' AND column_name='source_msg_id')=0,
  'ALTER TABLE wecom_aibot_identity ADD COLUMN source_msg_id VARCHAR(128) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_aibot_identity' AND column_name='first_seen_at')=0,
  'ALTER TABLE wecom_aibot_identity ADD COLUMN first_seen_at DATETIME(6) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_aibot_identity' AND column_name='last_seen_at')=0,
  'ALTER TABLE wecom_aibot_identity ADD COLUMN last_seen_at DATETIME(6) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_aibot_identity' AND column_name='revoked_at')=0,
  'ALTER TABLE wecom_aibot_identity ADD COLUMN revoked_at DATETIME(6) NULL', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@schema_name AND table_name='wecom_aibot_identity' AND column_name='identity_status')=0,
  'SELECT 1', 'ALTER TABLE wecom_aibot_identity MODIFY identity_status ENUM(''unverified'',''conversion_pending'',''verified'',''rejected'',''revoked'') NOT NULL DEFAULT ''unverified'''); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

CREATE TABLE IF NOT EXISTS aibot_identity_binding (
  binding_id CHAR(36) NOT NULL,
  bot_id VARCHAR(128) NOT NULL,
  opaque_actor_digest CHAR(64) NOT NULL,
  canonical_userid VARCHAR(64) NOT NULL,
  binding_status ENUM('pending','active','rejected','revoked') NOT NULL DEFAULT 'pending',
  binding_source ENUM('auto_verified','invite','pre_registered','admin') NOT NULL DEFAULT 'auto_verified',
  invite_id CHAR(36) NULL,
  approved_by VARCHAR(64) NULL,
  approved_at DATETIME(6) NULL,
  revoked_by VARCHAR(64) NULL,
  revoked_at DATETIME(6) NULL,
  version INT UNSIGNED NOT NULL DEFAULT 1,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (binding_id),
  UNIQUE KEY uk_aibot_binding_identity_status (bot_id, opaque_actor_digest, binding_status),
  UNIQUE KEY uk_aibot_binding_canonical_status (bot_id, canonical_userid, binding_status),
  KEY idx_aibot_binding_canonical (bot_id, canonical_userid, binding_status),
  CONSTRAINT fk_aibot_binding_user FOREIGN KEY (canonical_userid) REFERENCES user(external_userid) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS aibot_registration (
  registration_id CHAR(36) NOT NULL,
  canonical_userid VARCHAR(64) NULL,
  identity_binding_id CHAR(36) NOT NULL,
  registration_status ENUM('discovered','pending_role','active','rejected','revoked') NOT NULL DEFAULT 'discovered',
  registration_source ENUM('auto_worker','pre_registered','invite','admin') NOT NULL DEFAULT 'auto_worker',
  requested_role ENUM('worker','factory','broker') NULL,
  granted_role ENUM('worker','factory','broker') NULL,
  capability_snapshot JSON NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (registration_id), KEY idx_aibot_registration_user_status (canonical_userid, registration_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS aibot_role_invite (
  invite_id CHAR(36) NOT NULL,
  token_digest CHAR(64) NOT NULL,
  target_role ENUM('factory','broker') NOT NULL,
  expires_at DATETIME(6) NOT NULL,
  max_uses INT UNSIGNED NOT NULL DEFAULT 1,
  used_count INT UNSIGNED NOT NULL DEFAULT 0,
  created_by VARCHAR(64) NOT NULL,
  revoked_at DATETIME(6) NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (invite_id), UNIQUE KEY uk_aibot_invite_token (token_digest),
  KEY idx_aibot_invite_active (target_role, expires_at, revoked_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS aibot_identity_audit (
  audit_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  bot_id VARCHAR(128) NOT NULL,
  opaque_actor_digest CHAR(64) NOT NULL,
  canonical_userid VARCHAR(64) NULL,
  action VARCHAR(48) NOT NULL,
  result VARCHAR(32) NOT NULL,
  reason_code VARCHAR(64) NULL,
  actor VARCHAR(64) NULL,
  metadata JSON NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (audit_id), KEY idx_aibot_identity_audit_lookup (bot_id, opaque_actor_digest, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Backfill only non-sensitive metadata.  Existing phase14 rows remain valid.
UPDATE wecom_aibot_identity SET canonical_userid=COALESCE(canonical_userid, mapped_external_userid)
 WHERE canonical_userid IS NULL AND mapped_external_userid IS NOT NULL;

-- Duplicate preflight is intentionally fail-closed: the unique-index DDL
-- below must fail if historical duplicate active rows exist.  Operators must
-- reconcile those rows explicitly; this migration never deletes or picks a
-- winner silently.
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema=@schema_name AND table_name='aibot_identity_binding' AND index_name='uk_aibot_binding_canonical_status')=0,
  'ALTER TABLE aibot_identity_binding ADD UNIQUE KEY uk_aibot_binding_canonical_status (bot_id, canonical_userid, binding_status)', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema=@schema_name AND table_name='aibot_registration' AND index_name='uk_aibot_registration_binding')=0,
  'ALTER TABLE aibot_registration ADD UNIQUE KEY uk_aibot_registration_binding (identity_binding_id)', 'SELECT 1'); PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- DOWN is intentionally manual and guarded: export identity/audit rows first,
-- stop AIBot, then drop only these additive tables/columns.  Never alter User.
