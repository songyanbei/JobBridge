#!/usr/bin/env bash
# Phase 4 Demo 环境停止脚本
# 用法：bash scripts/demo_env_stop.sh

set -euo pipefail

SESSION="jobbridge-demo"

if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "[+] 停止 tmux 会话 $SESSION..."
    tmux kill-session -t "$SESSION"
fi

# 兜底 kill（防止 tmux 没清理干净）
pkill -f "cpolar http" 2>/dev/null || true
pkill -f "uvicorn app.main:app" 2>/dev/null || true
pkill -f "python -m app.services.worker" 2>/dev/null || true

echo "[✓] Demo 环境已停止"
echo "    如需清理测试数据：bash scripts/demo_env_cleanup.sh"
