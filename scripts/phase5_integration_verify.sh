#!/bin/bash
# Phase 5 WSL 集成验证脚本（整段执行，避免 backtick 嵌套）
set -euo pipefail

cd /mnt/d/work/JobBridge

echo "=== waiting for MySQL healthy ==="
for i in $(seq 1 30); do
  status=$(docker inspect --format '{{.State.Health.Status}}' jobbridge-mysql 2>/dev/null || echo none)
  if [ "$status" = "healthy" ]; then
    echo "MySQL healthy after $((i*2))s"
    break
  fi
  sleep 2
done

docker ps --format 'table {{.Names}}\t{{.Status}}' | head -5
echo

echo "=== verify schema reflects Phase 5 ==="
docker exec jobbridge-mysql sh -c 'mysql -uroot -proot -N -e "USE jobbridge; SHOW TABLES;"' 2>/dev/null | sort
echo
docker exec jobbridge-mysql sh -c 'mysql -uroot -proot -N -e "USE jobbridge; SELECT COLUMN_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=\"jobbridge\" AND TABLE_NAME=\"audit_log\" AND COLUMN_NAME=\"action\";"' 2>/dev/null
docker exec jobbridge-mysql sh -c 'mysql -uroot -proot -N -e "USE jobbridge; SELECT COLUMN_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=\"jobbridge\" AND TABLE_NAME=\"audit_log\" AND COLUMN_NAME=\"target_type\";"' 2>/dev/null

echo
echo "=== verify seed data ==="
docker exec jobbridge-mysql sh -c 'mysql -uroot -proot -N -e "USE jobbridge; SELECT config_key FROM system_config WHERE config_key IN (\"event.dedupe_window_seconds\",\"audit.lock_ttl_seconds\",\"audit.undo_window_seconds\",\"report.cache_ttl_seconds\",\"account.import_max_rows\");"' 2>/dev/null
echo
docker exec jobbridge-mysql sh -c 'mysql -uroot -proot -N -e "USE jobbridge; SELECT username, password_changed, enabled FROM admin_user;"' 2>/dev/null
