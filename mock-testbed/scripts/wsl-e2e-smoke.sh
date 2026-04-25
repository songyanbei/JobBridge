#!/usr/bin/env bash
# 端到端 smoke：mock /inbound → queue:incoming → Worker → LLM(qwen) → send_text → [MOCK-WEWORK] → Redis pubsub
# 验证完整链路 + 真实 LLM 调用
set -euo pipefail

ROOT=/mnt/d/work/JobBridge/.claude/worktrees/romantic-proskuriakova-bdc75a
VENV=/tmp/mock-testbed-venv
BACKEND="$ROOT/mock-testbed/backend"

# 1. 配置 mock-testbed .env 指向 host loopback（demo overlay 已暴露 3306/6379）
cat > "$BACKEND/.env" <<EOF
MOCK_DB_DSN=mysql+pymysql://jobbridge:jobbridge@127.0.0.1:3306/jobbridge?charset=utf8mb4
MOCK_REDIS_URL=redis://127.0.0.1:6379/0
MOCK_PORT=8001
MOCK_HOST=127.0.0.1
MOCK_CORS_ORIGINS=http://localhost:5174
MOCK_CORPID=wwmock_corpid
MOCK_AGENTID=1000002
EOF

# 2. 启 mock-testbed 后端
cd "$BACKEND"
mkdir -p /tmp/mock-testbed-stress-logs
nohup "$VENV/bin/uvicorn" main:app --host 127.0.0.1 --port 8001 \
    > /tmp/mock-testbed-stress-logs/backend.log 2>&1 &
MOCK_PID=$!
echo "$MOCK_PID" > /tmp/mock-testbed-stress-logs/backend.pid

# 等就绪
echo "==> 等 mock backend 就绪..."
for i in $(seq 1 20); do
    if curl -fsS http://127.0.0.1:8001/health >/dev/null 2>&1; then
        echo "  up after ${i}s (pid=$MOCK_PID)"
        break
    fi
    sleep 1
done
if ! curl -fsS http://127.0.0.1:8001/health >/dev/null 2>&1; then
    echo "❌ mock backend 没起来"
    tail -20 /tmp/mock-testbed-stress-logs/backend.log
    kill "$MOCK_PID" 2>/dev/null || true
    exit 1
fi

# 3. 启 SUBSCRIBE 监听 outbound channel（后台），最多 30s
TARGET_USER=wm_mock_worker_001
CHANNEL="mock:outbound:$TARGET_USER"
echo ""
echo "==> 后台 SUBSCRIBE Redis channel: $CHANNEL（最多 30s）"
docker exec jobbridge-redis timeout 30 redis-cli SUBSCRIBE "$CHANNEL" \
    > /tmp/mock-testbed-stress-logs/sse-channel.log 2>&1 &
SUB_PID=$!
sleep 1

# 4. 发一条文本消息
MSG_ID="e2e_smoke_$(date +%s)"
echo ""
echo "==> POST /mock/wework/inbound（msg_id=$MSG_ID, content='你好，我想找深圳的打包工'）"
RESP=$(curl -s -X POST http://127.0.0.1:8001/mock/wework/inbound \
    -H "Content-Type: application/json" \
    -d "{\"ToUserName\":\"wwmock_corpid\",\"FromUserName\":\"$TARGET_USER\",\"CreateTime\":$(date +%s),\"MsgType\":\"text\",\"Content\":\"你好，我想找深圳的打包工\",\"MsgId\":\"$MSG_ID\",\"AgentID\":\"1000002\"}")
echo "  响应: $RESP"

# 5. 等 LLM + Worker 处理（Bailian 大致 1-3s）
echo ""
echo "==> 等 Worker 消费 + LLM 调用 + 出站拦截（最多 25s）"
for i in $(seq 1 25); do
    if grep -qE "^[^[:space:]]" /tmp/mock-testbed-stress-logs/sse-channel.log 2>/dev/null && \
       grep -qE "msgtype|content" /tmp/mock-testbed-stress-logs/sse-channel.log; then
        echo "  ✅ Outbound 消息已到达（${i}s）"
        break
    fi
    sleep 1
done

# 6. 杀掉 SUBSCRIBE
kill "$SUB_PID" 2>/dev/null || true
wait "$SUB_PID" 2>/dev/null || true

# 7. 报告
echo ""
echo "============================================================"
echo "  E2E Smoke 结果"
echo "============================================================"

echo ""
echo "--- Redis SUBSCRIBE 收到的内容 ---"
cat /tmp/mock-testbed-stress-logs/sse-channel.log | tail -20
echo ""

echo "--- DB wecom_inbound_event 记录 ---"
docker exec jobbridge-mysql mysql -uroot -proot -se \
    "SELECT msg_id, status, LEFT(content_brief, 60) AS preview, error_message FROM jobbridge.wecom_inbound_event WHERE msg_id='$MSG_ID' \G" 2>&1 | grep -v "Warning"
echo ""

echo "--- DB conversation_log 记录 ---"
docker exec jobbridge-mysql mysql -uroot -proot -se \
    "SELECT direction, intent, LEFT(content, 80) AS content FROM jobbridge.conversation_log WHERE userid='$TARGET_USER' AND created_at >= NOW() - INTERVAL 1 MINUTE ORDER BY created_at \G" 2>&1 | grep -v "Warning"
echo ""

echo "--- Worker 日志 LLM 相关行（最后 30 行 grep）---"
docker logs --tail 200 jobbridge-worker-1 2>&1 | grep -iE "qwen|dashscope|llm|intent|send_text|MOCK-WEWORK|ERROR" | tail -20

# 8. 收尾
echo ""
echo "==> 关闭 mock backend"
kill "$MOCK_PID" 2>/dev/null || true
rm -f /tmp/mock-testbed-stress-logs/backend.pid

echo ""
echo "✅ smoke 跑完。日志: /tmp/mock-testbed-stress-logs/"
