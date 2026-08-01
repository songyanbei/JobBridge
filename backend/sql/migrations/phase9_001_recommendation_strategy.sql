-- Recommendation v1 control plane.  This migration is intentionally phase9:
-- phase8_001..005 are already used by conversation production hardening.
CREATE TABLE IF NOT EXISTS recommendation_strategy_version (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  direction VARCHAR(32) NOT NULL,
  version_no INT UNSIGNED NOT NULL,
  template_key VARCHAR(32) NOT NULL,
  status ENUM('draft','published','archived') NOT NULL DEFAULT 'draft',
  parameters JSON NOT NULL,
  parameters_digest CHAR(64) NOT NULL,
  last_simulated_digest CHAR(64) NULL,
  last_simulated_at DATETIME(6) NULL,
  algorithm_version VARCHAR(32) NOT NULL DEFAULT 'recommendation-v1',
  base_version_id BIGINT UNSIGNED NULL,
  lock_version INT UNSIGNED NOT NULL DEFAULT 1,
  change_reason VARCHAR(255) NOT NULL,
  created_by VARCHAR(64) NOT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  published_by VARCHAR(64) NULL,
  published_at DATETIME(6) NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_recommendation_version_direction_no (direction, version_no),
  KEY idx_recommendation_version_status (direction, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS recommendation_strategy_release (
  direction VARCHAR(32) NOT NULL,
  execution_mode ENUM('off','shadow','on') NOT NULL DEFAULT 'off',
  stable_version_id BIGINT UNSIGNED NULL,
  candidate_version_id BIGINT UNSIGNED NULL,
  rollout_percentage INT UNSIGNED NOT NULL DEFAULT 0,
  revision BIGINT UNSIGNED NOT NULL DEFAULT 1,
  lock_version INT UNSIGNED NOT NULL DEFAULT 1,
  updated_by VARCHAR(64) NOT NULL DEFAULT 'system',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (direction)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS recommendation_release_history (
  direction VARCHAR(32) NOT NULL,
  revision BIGINT UNSIGNED NOT NULL,
  operation VARCHAR(32) NOT NULL,
  execution_mode VARCHAR(16) NOT NULL,
  stable_version_id BIGINT UNSIGNED NULL,
  candidate_version_id BIGINT UNSIGNED NULL,
  rollout_percentage INT UNSIGNED NOT NULL,
  target_revision BIGINT UNSIGNED NULL,
  change_reason VARCHAR(255) NOT NULL,
  created_by VARCHAR(64) NOT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (direction, revision)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS recommendation_runtime_control (
  scope VARCHAR(16) NOT NULL,
  kill_switch TINYINT(1) NOT NULL DEFAULT 0,
  revision BIGINT UNSIGNED NOT NULL DEFAULT 1,
  lock_version INT UNSIGNED NOT NULL DEFAULT 1,
  change_reason VARCHAR(255) NOT NULL DEFAULT 'initial',
  updated_by VARCHAR(64) NOT NULL DEFAULT 'system',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (scope)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT IGNORE INTO recommendation_strategy_release
  (direction, execution_mode, rollout_percentage, revision, lock_version, updated_by)
VALUES ('search_job','off',0,1,1,'system'), ('search_worker','off',0,1,1,'system');
INSERT IGNORE INTO recommendation_runtime_control
  (scope, kill_switch, revision, lock_version, change_reason, updated_by)
VALUES ('global',0,1,1,'initial','system');
INSERT IGNORE INTO recommendation_release_history
  (direction, revision, operation, execution_mode, rollout_percentage, change_reason, created_by)
VALUES
  ('search_job',1,'init','off',0,'initial legacy baseline','system'),
  ('search_worker',1,'init','off',0,'initial legacy baseline','system');
