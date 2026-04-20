#!/usr/bin/env bash
# All-in-one: start mock-testbed backend, smoke, stress, cleanup, report.
# Assumes venv at /tmp/mock-testbed-venv already populated by wsl-stress-setup.sh.
set -euo pipefail

ROOT=/mnt/d/work/JobBridge/.claude/worktrees/romantic-proskuriakova-bdc75a
VENV=/tmp/mock-testbed-venv
BACKEND="$ROOT/mock-testbed/backend"
DRIVER="$ROOT/mock-testbed/scripts/wsl-stress-driver.py"
LOG_DIR=/tmp/mock-testbed-stress-logs
BACKEND_LOG="$LOG_DIR/backend.log"
BACKEND_PID_FILE="$LOG_DIR/backend.pid"
REPORT="$LOG_DIR/report.md"
TARGET="http://127.0.0.1:8001"

mkdir -p "$LOG_DIR"
rm -f "$REPORT"

cleanup() {
    if [ -f "$BACKEND_PID_FILE" ]; then
        kill "$(cat "$BACKEND_PID_FILE")" 2>/dev/null || true
        rm -f "$BACKEND_PID_FILE"
    fi
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# 0. Env
# ---------------------------------------------------------------------------
cd "$BACKEND"
cat > /tmp/mock-testbed-stress.env <<EOF
MOCK_DB_DSN=mysql+pymysql://jobbridge:jobbridge@127.0.0.1:3306/jobbridge
MOCK_REDIS_URL=redis://127.0.0.1:6379/0
MOCK_PORT=8001
MOCK_HOST=127.0.0.1
MOCK_CORS_ORIGINS=http://localhost:5174,http://127.0.0.1:5174
MOCK_CORPID=wwmock_corpid
MOCK_AGENTID=1000002
EOF

# copy env to backend dir so config.py picks it up
cp /tmp/mock-testbed-stress.env "$BACKEND/.env"

# ---------------------------------------------------------------------------
# 1. Start mock-testbed backend
# ---------------------------------------------------------------------------
echo "=== [1] Starting mock-testbed backend on $TARGET ==="
nohup "$VENV/bin/uvicorn" main:app --host 127.0.0.1 --port 8001 \
    > "$BACKEND_LOG" 2>&1 &
echo $! > "$BACKEND_PID_FILE"

# wait for readiness
for i in $(seq 1 30); do
    if curl -fsS "$TARGET/health" > /dev/null 2>&1; then
        echo "  backend up (after ${i}s)"
        break
    fi
    sleep 1
done

if ! curl -fsS "$TARGET/health" > /dev/null; then
    echo "  ❌ backend did not come up; log tail:"
    tail -20 "$BACKEND_LOG"
    exit 1
fi

# ---------------------------------------------------------------------------
# 2. Smoke tests (B)
# ---------------------------------------------------------------------------
echo ""
echo "=== [2] Smoke tests ==="
{
    echo "## Smoke"
    echo ""
    echo "### /health"
    curl -s "$TARGET/health"
    echo ""
    echo ""
    echo "### /mock/wework/config"
    curl -s "$TARGET/mock/wework/config"
    echo ""
    echo ""
    echo "### /mock/wework/users (显示 4 个 wm_mock_* 身份)"
    curl -s "$TARGET/mock/wework/users"
    echo ""
    echo ""
    echo "### /mock/wework/oauth2/authorize (302)"
    curl -s -D - -o /dev/null -X GET --max-redirs 0 \
        "$TARGET/mock/wework/oauth2/authorize?appid=wwX&redirect_uri=http%3A%2F%2Fexample.com%2Fcb&state=abc" \
        | head -8
    echo ""
    echo "### /mock/wework/code2userinfo"
    curl -s "$TARGET/mock/wework/code2userinfo?access_token=MOCK&code=xyz"
    echo ""
    echo ""
    echo "### /mock/wework/inbound (smoke 1 条)"
    curl -s -X POST "$TARGET/mock/wework/inbound" \
        -H "Content-Type: application/json" \
        -d '{"ToUserName":"wwmock_corpid","FromUserName":"wm_mock_worker_001","CreateTime":'"$(date +%s)"',"MsgType":"text","Content":"smoke","MsgId":"smoke_'"$(date +%s)"'","AgentID":"1000002"}'
    echo ""
    echo ""
    echo "### /mock/wework/inbound 拒非 wm_mock_ 前缀"
    curl -s -X POST "$TARGET/mock/wework/inbound" \
        -H "Content-Type: application/json" \
        -d '{"ToUserName":"c","FromUserName":"real_attacker","CreateTime":1,"MsgType":"text","Content":"x","MsgId":"x1","AgentID":"a"}'
    echo ""
    echo ""
    echo "### /mock/wework/sse 响应头（只取前 5 行）"
    curl -s -D - -o /dev/null --max-time 2 \
        "$TARGET/mock/wework/sse?external_userid=wm_mock_worker_001" | head -8 || true
} > "$LOG_DIR/smoke.txt" 2>&1
cat "$LOG_DIR/smoke.txt" | tail -60

# ---------------------------------------------------------------------------
# 3. Throughput test (C)
# ---------------------------------------------------------------------------
echo ""
echo "=== [3] Throughput test: 1000 requests @ concurrency 50 ==="
"$VENV/bin/python" "$DRIVER" --total 1000 --concurrency 50 --target "$TARGET" \
    2>&1 | tee "$LOG_DIR/stress.txt"

# ---------------------------------------------------------------------------
# 4. Post-mortem: DB rows + Redis state
# ---------------------------------------------------------------------------
echo ""
echo "=== [4] Post-mortem ==="
{
    echo "## DB inbound events from this run:"
    docker exec jobbridge-mysql mysql -uroot -proot -se \
        "SELECT COUNT(*) AS events, MIN(created_at), MAX(created_at) FROM jobbridge.wecom_inbound_event WHERE msg_id LIKE 'stress_%';"
    echo ""
    echo "## Status breakdown:"
    docker exec jobbridge-mysql mysql -uroot -proot -se \
        "SELECT status, COUNT(*) FROM jobbridge.wecom_inbound_event WHERE msg_id LIKE 'stress_%' GROUP BY status;"
    echo ""
    echo "## Redis queue:incoming:"
    docker exec jobbridge-redis redis-cli LLEN queue:incoming
    echo ""
    echo "## Redis mock:outbound:* channel count:"
    docker exec jobbridge-redis redis-cli PUBSUB CHANNELS "mock:outbound:*"
} > "$LOG_DIR/postmortem.txt" 2>&1
cat "$LOG_DIR/postmortem.txt"

# ---------------------------------------------------------------------------
# 5. Cleanup: delete stress_* rows so this doesn't pollute main test data
# ---------------------------------------------------------------------------
echo ""
echo "=== [5] Cleanup stress_* DB rows ==="
docker exec jobbridge-mysql mysql -uroot -proot -e \
    "DELETE FROM jobbridge.wecom_inbound_event WHERE msg_id LIKE 'stress_%';" 2>&1 | tail -2 || true

echo ""
echo "=== done. Logs: $LOG_DIR ==="
