-- Execute only after the phase14 release gate and consumer shutdown.
ALTER TABLE `job` DROP KEY `idx_job_lifecycle_version`;
