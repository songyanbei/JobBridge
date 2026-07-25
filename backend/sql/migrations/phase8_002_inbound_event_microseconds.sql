-- Preserve sub-second queue/process timing in the durable inbound event table.
-- Safe to run repeatedly on MySQL 8.0.

SET @schema_name = DATABASE();
SET @needs_fsp6 = (
    SELECT COUNT(*)
      FROM information_schema.columns
     WHERE table_schema = @schema_name
       AND table_name = 'wecom_inbound_event'
       AND column_name IN ('created_at', 'worker_started_at', 'worker_finished_at')
       AND datetime_precision <> 6
);

SET @ddl = IF(
    @needs_fsp6 = 0,
    'SELECT 1',
    'ALTER TABLE `wecom_inbound_event`
       MODIFY COLUMN `worker_started_at` DATETIME(6) NULL COMMENT ''Worker 开始处理时间'',
       MODIFY COLUMN `worker_finished_at` DATETIME(6) NULL COMMENT ''Worker 处理完成时间'',
       MODIFY COLUMN `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT ''回调到达时间'''
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
