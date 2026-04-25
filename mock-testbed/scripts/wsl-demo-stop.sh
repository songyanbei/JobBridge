#!/usr/bin/env bash
# 停 mock-testbed 前后端
LOG_DIR=/tmp/mock-testbed-stress-logs

for name in backend frontend; do
    PID_FILE="$LOG_DIR/${name}.pid"
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill "$PID" 2>/dev/null; then
            echo "✅ $name (pid=$PID) 已停"
        else
            echo "⚠️  $name (pid=$PID) 已不在运行"
        fi
        rm -f "$PID_FILE"
    fi
done

# 兜底：杀残留
pkill -f "uvicorn main:app" 2>/dev/null || true
pkill -f "vite" 2>/dev/null || true
echo "兜底清理完成"
