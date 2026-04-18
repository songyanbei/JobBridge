#!/usr/bin/env bash
# ============================================================================
# MySQL 定时备份脚本（Phase 7 §3.1 模块 K）
#
# 用途：每日 03:30 被 crontab 调起，把 jobbridge 数据库 dump 到 .sql.gz，
#       保留 14 天（超过 14 天自动清理）。
#
# crontab 示例：
#   30 3 * * * /opt/jobbridge/scripts/backup_mysql.sh >> /var/log/jobbridge/backup.log 2>&1
#
# 环境变量：
#   BACKUP_DIR            备份目录（默认 /data/jobbridge/backup/mysql）
#   MYSQL_ROOT_PASSWORD   MySQL root 密码（必填）
#   DB_NAME               数据库名（默认 jobbridge）
#   MYSQL_CONTAINER       docker 容器名（默认 jobbridge-mysql）
#
# 安全约束：密码不会被写进日志或备份文件名。
# ============================================================================
set -euo pipefail

TS=$(date +%Y%m%d_%H%M%S)
OUT_DIR=${BACKUP_DIR:-/data/jobbridge/backup/mysql}
DB_NAME=${DB_NAME:-jobbridge}
MYSQL_CONTAINER=${MYSQL_CONTAINER:-jobbridge-mysql}

if [[ -z "${MYSQL_ROOT_PASSWORD:-}" ]]; then
  echo "[backup_mysql] FATAL: MYSQL_ROOT_PASSWORD is empty" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"

OUT_FILE="$OUT_DIR/jobbridge_${TS}.sql.gz"
echo "[backup_mysql] start: db=$DB_NAME → $OUT_FILE"

# 通过环境变量传递密码，避免出现在 ps / 日志中
docker exec -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" "$MYSQL_CONTAINER" \
  mysqldump -u root \
    --single-transaction \
    --routines --events \
    --default-character-set=utf8mb4 \
    "$DB_NAME" \
  | gzip > "$OUT_FILE"

# 校验非空
if [[ ! -s "$OUT_FILE" ]]; then
  echo "[backup_mysql] FATAL: dump file is empty: $OUT_FILE" >&2
  rm -f "$OUT_FILE"
  exit 2
fi

SIZE=$(du -h "$OUT_FILE" | awk '{print $1}')
echo "[backup_mysql] done: size=$SIZE"

# 保留 14 天
find "$OUT_DIR" -maxdepth 1 -type f -name 'jobbridge_*.sql.gz' -mtime +14 -print -delete

echo "[backup_mysql] retention: older-than-14d cleaned"
