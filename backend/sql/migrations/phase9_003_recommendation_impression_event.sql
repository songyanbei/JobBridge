CREATE TABLE IF NOT EXISTS recommendation_impression (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  delivery_id CHAR(36) NOT NULL,
  request_id CHAR(36) NOT NULL,
  snapshot_id CHAR(36) NOT NULL,
  viewer_userid VARCHAR(64) NOT NULL,
  direction VARCHAR(32) NOT NULL,
  target_type VARCHAR(16) NOT NULL,
  target_id BIGINT UNSIGNED NOT NULL,
  position SMALLINT UNSIGNED NOT NULL,
  strategy_version_id BIGINT UNSIGNED NULL,
  algorithm_version VARCHAR(32) NOT NULL,
  assignment VARCHAR(16) NOT NULL,
  is_exploration TINYINT(1) NOT NULL DEFAULT 0,
  query_digest VARCHAR(16) NOT NULL,
  score_detail JSON NULL,
  exposed_at DATETIME(6) NOT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uk_recommendation_impression_delivery_target (delivery_id, target_type, target_id),
  KEY idx_recommendation_impression_viewer_time (viewer_userid, target_type, exposed_at),
  KEY idx_recommendation_impression_target_time (target_type, target_id, exposed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

ALTER TABLE recommendation_impression
  ADD CONSTRAINT fk_recommendation_impression_delivery
  FOREIGN KEY (delivery_id) REFERENCES recommendation_delivery(delivery_id),
  ADD CONSTRAINT fk_recommendation_impression_request
  FOREIGN KEY (request_id) REFERENCES recommendation_request(request_id);

CREATE TABLE IF NOT EXISTS recommendation_exposure_daily (
  stat_date DATE NOT NULL,
  target_type VARCHAR(16) NOT NULL,
  target_id BIGINT UNSIGNED NOT NULL,
  impression_count INT UNSIGNED NOT NULL DEFAULT 0,
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (stat_date, target_type, target_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

ALTER TABLE event_log
  ADD COLUMN IF NOT EXISTS delivery_id CHAR(36) NULL,
  ADD COLUMN IF NOT EXISTS request_id CHAR(36) NULL,
  ADD COLUMN IF NOT EXISTS snapshot_id CHAR(36) NULL,
  ADD COLUMN IF NOT EXISTS position SMALLINT UNSIGNED NULL,
  ADD COLUMN IF NOT EXISTS attribution_status VARCHAR(24) NOT NULL DEFAULT 'legacy_unattributed',
  ADD COLUMN IF NOT EXISTS attributed_strategy_version_id BIGINT UNSIGNED NULL,
  ADD COLUMN IF NOT EXISTS attributed_algorithm_version VARCHAR(32) NULL,
  ADD COLUMN IF NOT EXISTS attributed_is_exploration TINYINT(1) NULL,
  ADD COLUMN IF NOT EXISTS client_event_id VARCHAR(64) NULL,
  ADD COLUMN IF NOT EXISTS attribution_dedupe_key CHAR(64) NULL,
  ADD UNIQUE KEY uk_event_attribution_dedupe (attribution_dedupe_key);
