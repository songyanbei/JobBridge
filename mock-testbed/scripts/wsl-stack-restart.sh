#!/usr/bin/env bash
# 重启 prod compose 栈，让新加的 LLM_API_KEY + MOCK_WEWORK_OUTBOUND
# 通过 .env (env_file) 注入到 app + worker 容器
set -euo pipefail

ROOT=/mnt/d/work/JobBridge/.claude/worktrees/romantic-proskuriakova-bdc75a
cd "$ROOT"

# 用主项目名 jobbridge 复用已有的 jobbridge_mysql_data / jobbridge_redis_data 等 volume
# 这些 volume 里有完整 schema + 已 seed 的字典/管理员/mock 用户
PROJ=jobbridge

echo "==> [1/5] 当前容器状态（项目: $PROJ）"
docker compose -p "$PROJ" -f docker-compose.prod.yml ps --format "table {{.Name}}\t{{.State}}\t{{.Status}}" 2>&1 || true

echo ""
echo "==> [2/5] 停掉 prod 栈"
docker compose -p "$PROJ" -f docker-compose.prod.yml -f docker-compose.demo.yml down 2>&1 | tail -5

echo ""
echo "==> [3/5] 重新拉起（读 .env：APP_ENV=development + MOCK_WEWORK_OUTBOUND=true + LLM_API_KEY）"
docker compose -p "$PROJ" -f docker-compose.prod.yml -f docker-compose.demo.yml up -d --build 2>&1 | tail -10

echo ""
echo "==> [4/5] 等待 healthy（最多 60s）"
for i in $(seq 1 30); do
    healthy=$(docker compose -p "$PROJ" -f docker-compose.prod.yml -f docker-compose.demo.yml ps --format json 2>/dev/null | grep -c '"Health":"healthy"' || true)
    total=$(docker compose -p "$PROJ" -f docker-compose.prod.yml -f docker-compose.demo.yml ps --format json 2>/dev/null | grep -c '"Service"' || true)
    echo "  t+${i}s healthy=$healthy/$total"
    [ "$healthy" -ge 4 ] 2>/dev/null && break
    sleep 2
done

echo ""
echo "==> [5/5] 验证环境变量已注入"
echo "--- jobbridge-app 关键 env ---"
docker exec jobbridge-app env 2>/dev/null | grep -E "^(APP_ENV|MOCK_WEWORK|LLM_API_KEY|LLM_API_BASE|LLM_PROVIDER|LLM_INTENT_MODEL)=" | sort
echo ""
echo "--- jobbridge-worker 关键 env ---"
docker exec jobbridge-worker-1 env 2>/dev/null | grep -E "^(APP_ENV|MOCK_WEWORK|LLM_API_KEY|LLM_API_BASE|LLM_PROVIDER|LLM_INTENT_MODEL)=" | sort \
  || docker exec "$(docker ps --filter name=worker --format '{{.Names}}' | head -1)" env 2>/dev/null | grep -E "^(APP_ENV|MOCK_WEWORK|LLM_API_KEY|LLM_API_BASE|LLM_PROVIDER|LLM_INTENT_MODEL)=" | sort

echo ""
echo "==> 容器最终状态"
docker compose -p "$PROJ" -f docker-compose.prod.yml -f docker-compose.demo.yml ps --format "table {{.Name}}\t{{.State}}\t{{.Health}}"

echo ""
echo "==> Worker 启动后的日志（最后 20 行）"
docker logs --tail 20 "$(docker ps --filter name=worker --format '{{.Names}}' | head -1)" 2>&1 | tail -20

echo ""
echo "✅ 完成"
