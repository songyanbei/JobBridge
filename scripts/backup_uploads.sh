#!/usr/bin/env bash
# ============================================================================
# 上传目录定时备份脚本（Phase 7 §3.1 模块 K）
#
# 用途：每日 04:00 打包 uploads/ 到宿主机备份目录，保留 14 天。
#
# crontab 示例：
#   0 4 * * * /opt/jobbridge/scripts/backup_uploads.sh >> /var/log/jobbridge/backup.log 2>&1
#
# 环境变量：
#   BACKUP_DIR    备份输出目录（默认 /data/jobbridge/backup/uploads）
#   UPLOADS_DIR   容器内 uploads 路径（默认 /data/uploads，docker volume 挂载点）
#   APP_CONTAINER docker 容器名（默认 jobbridge-app）
# ============================================================================
set -euo pipefail

TS=$(date +%Y%m%d_%H%M%S)
OUT_DIR=${BACKUP_DIR:-/data/jobbridge/backup/uploads}
UPLOADS_DIR=${UPLOADS_DIR:-/data/uploads}
APP_CONTAINER=${APP_CONTAINER:-jobbridge-app}

mkdir -p "$OUT_DIR"
OUT_FILE="$OUT_DIR/uploads_${TS}.tar.gz"
echo "[backup_uploads] start → $OUT_FILE"

# 用 docker exec tar 避免 volume 权限问题
docker exec "$APP_CONTAINER" \
  tar -czf - -C "$UPLOADS_DIR" . \
  > "$OUT_FILE"

if [[ ! -s "$OUT_FILE" ]]; then
  echo "[backup_uploads] WARN: archive is empty (uploads dir may be empty): $OUT_FILE" >&2
fi

SIZE=$(du -h "$OUT_FILE" | awk '{print $1}')
echo "[backup_uploads] done: size=$SIZE"

# 保留 14 天
find "$OUT_DIR" -maxdepth 1 -type f -name 'uploads_*.tar.gz' -mtime +14 -print -delete

echo "[backup_uploads] retention: older-than-14d cleaned"
