#!/usr/bin/env bash
# Phase 4 Demo 测试数据清理脚本
# 用法：bash scripts/demo_env_cleanup.sh
#
# 作用：清掉 demo_* / test_e2e_* 相关的 MySQL 数据与 Redis key

set -euo pipefail

echo "[+] 清理 MySQL Demo 数据..."
docker exec -i jobbridge-mysql mysql --default-character-set=utf8mb4 -u jobbridge -pjobbridge jobbridge <<'SQL'
DELETE FROM conversation_log
  WHERE userid LIKE 'demo_%' OR userid LIKE 'test_e2e_%';
DELETE FROM wecom_inbound_event
  WHERE from_userid LIKE 'demo_%' OR from_userid LIKE 'test_e2e_%';
DELETE FROM audit_log
  WHERE target_id LIKE 'demo_%' OR operator LIKE 'demo_%';
DELETE FROM job
  WHERE owner_userid LIKE 'demo_%' OR owner_userid LIKE 'test_e2e_%';
DELETE FROM resume
  WHERE owner_userid LIKE 'demo_%' OR owner_userid LIKE 'test_e2e_%';
DELETE FROM user
  WHERE external_userid LIKE 'demo_%' OR external_userid LIKE 'test_e2e_%';
SELECT 'user'                AS tbl, COUNT(*) FROM user  WHERE external_userid LIKE 'demo_%'
UNION ALL SELECT 'job',                COUNT(*) FROM job   WHERE owner_userid LIKE 'demo_%'
UNION ALL SELECT 'resume',             COUNT(*) FROM resume WHERE owner_userid LIKE 'demo_%'
UNION ALL SELECT 'conversation_log',   COUNT(*) FROM conversation_log WHERE userid LIKE 'demo_%'
UNION ALL SELECT 'wecom_inbound_event', COUNT(*) FROM wecom_inbound_event WHERE from_userid LIKE 'demo_%';
SQL

echo ""
echo "[+] 清理 Redis Demo key..."
for pat in "session:demo_*" "lock:demo_*" "rate:demo_*" "rate_limit_notified:demo_*" \
           "session:test_e2e_*" "lock:test_e2e_*" "rate:test_e2e_*"; do
    keys=$(docker exec jobbridge-redis redis-cli --scan --pattern "$pat" 2>/dev/null || true)
    if [ -n "$keys" ]; then
        echo "$keys" | xargs -r docker exec -i jobbridge-redis redis-cli DEL
    fi
done

# 清 Phase 4 队列（安全：队列里积压的都是测试消息）
echo "[+] 清理 Redis 队列..."
docker exec jobbridge-redis redis-cli DEL \
    queue:incoming queue:dead_letter queue:send_retry queue:rate_limit_notify

echo ""
echo "[✓] 清理完成"
