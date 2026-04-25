#!/usr/bin/env bash
# 启 mock-testbed 后端 + 前端（demo 用）
# 后端走 WSL-local /tmp/mock-testbed-venv（已装），前端首次会 npm install
set -e

ROOT=/mnt/d/work/JobBridge/.claude/worktrees/romantic-proskuriakova-bdc75a
VENV=/tmp/mock-testbed-venv
BACKEND="$ROOT/mock-testbed/backend"
FRONTEND="$ROOT/mock-testbed/frontend"
LOG_DIR=/tmp/mock-testbed-stress-logs

mkdir -p "$LOG_DIR"

# 杀掉旧实例
if [ -f "$LOG_DIR/backend.pid" ]; then
    kill "$(cat "$LOG_DIR/backend.pid")" 2>/dev/null || true
fi
if [ -f "$LOG_DIR/frontend.pid" ]; then
    kill "$(cat "$LOG_DIR/frontend.pid")" 2>/dev/null || true
fi
sleep 1

# 后端 —— 用 setsid 让进程脱离当前 session，避免 wsl 关闭时被连带杀
echo "==> 启 mock-testbed backend (端口 8001)"
cd "$BACKEND"
setsid nohup "$VENV/bin/uvicorn" main:app --host 0.0.0.0 --port 8001 \
    < /dev/null > "$LOG_DIR/backend.log" 2>&1 &
echo $! > "$LOG_DIR/backend.pid"
echo "  pid=$(cat "$LOG_DIR/backend.pid")"

# 等就绪
for i in $(seq 1 20); do
    curl -fsS http://127.0.0.1:8001/health >/dev/null 2>&1 && { echo "  ✅ backend up after ${i}s"; break; }
    sleep 1
done

# 前端：node_modules 在 Windows mount 装巨慢；放 /tmp 用 cp -al 链接（fallback 软链）
echo ""
echo "==> 启 mock-testbed frontend (端口 5174)"
TMP_FE=/tmp/mock-testbed-frontend
if [ ! -d "$TMP_FE" ]; then
    echo "  首次：复制前端到 WSL-local $TMP_FE 加速 npm install"
    mkdir -p "$TMP_FE"
    cp -r "$FRONTEND"/* "$TMP_FE/"
    cp "$FRONTEND"/.env.example "$TMP_FE/" 2>/dev/null || true
fi
cd "$TMP_FE"
if [ ! -d node_modules ]; then
    echo "  npm install（首次需 1-3 分钟）..."
    npm install --silent 2>&1 | tail -5
fi
setsid nohup npm run dev -- --host 0.0.0.0 --port 5174 \
    < /dev/null > "$LOG_DIR/frontend.log" 2>&1 &
echo $! > "$LOG_DIR/frontend.pid"
echo "  pid=$(cat "$LOG_DIR/frontend.pid")"

# 等 vite 就绪
for i in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:5174 >/dev/null 2>&1; then
        echo "  ✅ frontend up after ${i}s"
        break
    fi
    sleep 1
done

echo ""
echo "============================================================"
echo "  服务全部启动完毕"
echo "============================================================"
echo "  Mock UI（双视角）:  http://localhost:5174"
echo "  Mock UI 单视角:     http://localhost:5174/single?external_userid=wm_mock_worker_001&role=worker"
echo "  Mock backend:       http://localhost:8001/docs"
echo "  Admin 后台:         http://localhost:8000/admin/login (admin / DemoPass2026!)"
echo "  日志:               $LOG_DIR/{backend,frontend}.log"
echo ""
echo "  停止：./mock-testbed/scripts/wsl-demo-stop.sh 或手动 kill PID"
