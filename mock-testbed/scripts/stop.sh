#!/usr/bin/env bash
# Mock 企业微信测试台 · 停止脚本
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOGS_DIR="$ROOT_DIR/logs"

stop_pid_file() {
    local pid_file="$1"
    local name="$2"
    if [ -f "$pid_file" ]; then
        local pid
        pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            echo "▶ 停止 $name (pid=$pid)..."
            kill "$pid" 2>/dev/null || true
            sleep 1
            if kill -0 "$pid" 2>/dev/null; then
                kill -9 "$pid" 2>/dev/null || true
            fi
        else
            echo "  $name (pid=$pid) 已不在运行"
        fi
        rm -f "$pid_file"
    else
        echo "  $name 未启动或 pid 文件缺失"
    fi
}

stop_pid_file "$LOGS_DIR/backend.pid" "mock-backend"
stop_pid_file "$LOGS_DIR/frontend.pid" "mock-frontend"

echo ""
echo "============================================================"
echo "  ✅ Mock 测试台已停止"
echo "============================================================"
echo ""
echo "⚠️  记得在主后端取消环境变量："
echo ""
echo "    unset MOCK_WEWORK_OUTBOUND"
echo "    unset MOCK_WEWORK_REDIS_URL"
echo ""
echo "============================================================"
