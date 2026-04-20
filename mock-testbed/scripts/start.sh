#!/usr/bin/env bash
# Mock 企业微信测试台 · 一键启动
#
# 约定：从 mock-testbed/ 目录任意位置调用都可以
# 后端跑 8001（可用 MOCK_PORT 覆盖）；前端跑 5174

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
FRONTEND_DIR="$ROOT_DIR/frontend"
LOGS_DIR="$ROOT_DIR/logs"

MOCK_PORT="${MOCK_PORT:-8001}"
FRONTEND_PORT=5174

mkdir -p "$LOGS_DIR"

# ---------------------------------------------------------------------------
# 前置提醒
# ---------------------------------------------------------------------------
echo ""
echo "============================================================"
echo "  Mock 企业微信测试台 — 启动脚本"
echo "============================================================"
echo ""
echo "⚠️  请务必在主后端进程启动前设置环境变量："
echo ""
echo "    export MOCK_WEWORK_OUTBOUND=true"
echo "    export MOCK_WEWORK_REDIS_URL=redis://localhost:6379/0"
echo ""
echo "否则主后端 WeComClient 出站拦截不生效，SSE 收不到 bot 回复。"
echo "============================================================"
echo ""

# ---------------------------------------------------------------------------
# 检查端口占用
# ---------------------------------------------------------------------------
check_port() {
    local port="$1"
    local name="$2"
    if command -v lsof >/dev/null 2>&1; then
        if lsof -i ":$port" >/dev/null 2>&1; then
            echo "❌ 端口 $port（$name）已被占用，请先释放或修改端口"
            exit 1
        fi
    elif command -v netstat >/dev/null 2>&1; then
        if netstat -an 2>/dev/null | grep -q ":$port .*LISTEN"; then
            echo "❌ 端口 $port（$name）已被占用，请先释放或修改端口"
            exit 1
        fi
    fi
}
check_port "$MOCK_PORT" "mock-backend"
check_port "$FRONTEND_PORT" "mock-frontend"

# ---------------------------------------------------------------------------
# 启动后端
# ---------------------------------------------------------------------------
echo "▶ 启动 mock backend（端口 $MOCK_PORT）..."
cd "$BACKEND_DIR"

if [ ! -d ".venv" ]; then
    echo "  首次运行，创建 venv..."
    python -m venv .venv || python3 -m venv .venv
fi

# 兼容 Windows (Scripts) 与 Unix (bin) 两种 venv 布局
if [ -f ".venv/Scripts/activate" ]; then
    # shellcheck disable=SC1091
    source .venv/Scripts/activate
    PY_EXE=".venv/Scripts/python"
elif [ -f ".venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
    PY_EXE=".venv/bin/python"
else
    echo "❌ venv 激活脚本找不到，删除 .venv 重建试试"
    exit 1
fi

echo "  安装依赖..."
"$PY_EXE" -m pip install -q --upgrade pip
"$PY_EXE" -m pip install -q -r requirements.txt

if [ ! -f ".env" ]; then
    echo "  未找到 .env，复制 .env.example..."
    cp .env.example .env
fi

echo "  启动 uvicorn..."
nohup "$PY_EXE" -m uvicorn main:app --host 0.0.0.0 --port "$MOCK_PORT" \
    > "$LOGS_DIR/backend.log" 2>&1 &
BACKEND_PID=$!
echo "$BACKEND_PID" > "$LOGS_DIR/backend.pid"
echo "  ✅ backend pid=$BACKEND_PID"

deactivate 2>/dev/null || true

# ---------------------------------------------------------------------------
# 启动前端
# ---------------------------------------------------------------------------
echo ""
echo "▶ 启动 mock frontend（端口 $FRONTEND_PORT）..."
cd "$FRONTEND_DIR"

if [ ! -d "node_modules" ]; then
    echo "  首次运行，安装 npm 依赖..."
    npm install
fi

nohup npm run dev > "$LOGS_DIR/frontend.log" 2>&1 &
FRONTEND_PID=$!
echo "$FRONTEND_PID" > "$LOGS_DIR/frontend.pid"
echo "  ✅ frontend pid=$FRONTEND_PID"

# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
echo ""
echo "============================================================"
echo "  ✅ Mock 测试台已启动"
echo "============================================================"
echo "  backend:  http://localhost:$MOCK_PORT"
echo "  frontend: http://localhost:$FRONTEND_PORT"
echo "  logs:     $LOGS_DIR/backend.log, $LOGS_DIR/frontend.log"
echo ""
echo "  停止：./scripts/stop.sh"
echo "  冒烟：./scripts/smoke.sh"
echo "============================================================"
