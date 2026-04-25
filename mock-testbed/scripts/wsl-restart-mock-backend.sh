#!/usr/bin/env bash
# 重启 mock-testbed backend，frontend 不动
LOG_DIR=/tmp/mock-testbed-stress-logs
ROOT=/mnt/d/work/JobBridge/.claude/worktrees/romantic-proskuriakova-bdc75a
VENV=/tmp/mock-testbed-venv

# 杀旧
PID=$(cat "$LOG_DIR/backend.pid" 2>/dev/null)
[ -n "$PID" ] && kill "$PID" 2>/dev/null
pkill -f "uvicorn main:app" 2>/dev/null || true
sleep 1

# 起新
cd "$ROOT/mock-testbed/backend"
setsid nohup "$VENV/bin/uvicorn" main:app --host 0.0.0.0 --port 8001 \
    < /dev/null > "$LOG_DIR/backend.log" 2>&1 &
echo $! > "$LOG_DIR/backend.pid"
sleep 2

echo "=== /health ==="
curl -s http://127.0.0.1:8001/health
echo ""
echo ""
echo "=== /mock/wework/users（中文应正确）==="
curl -s http://127.0.0.1:8001/mock/wework/users | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f\"errcode={d['errcode']}, total {len(d['users'])} users\")
for u in d['users']:
    print(f\"  {u['external_userid']:25} role={u['role']:8} name={u['name']}\")
"
