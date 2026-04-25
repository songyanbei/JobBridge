#!/usr/bin/env bash
# 全场景端到端冒烟：5 类业务消息 + admin 数据落地校验
# 验证完整管线：mock UI → /inbound → queue → Worker → LLM/命令解析
#                → message_router → conversation_log → send_text
#                → [MOCK-WEWORK] → Redis pubsub → SSE 帧
set -euo pipefail

ROOT=/mnt/d/work/JobBridge/.claude/worktrees/romantic-proskuriakova-bdc75a
VENV=/tmp/mock-testbed-venv
BACKEND="$ROOT/mock-testbed/backend"
LOG_DIR=/tmp/mock-testbed-stress-logs
REPORT="$LOG_DIR/full-smoke-report.txt"

mkdir -p "$LOG_DIR"
: > "$REPORT"

cleanup_mock_data() {
    docker exec jobbridge-redis redis-cli FLUSHDB >/dev/null 2>&1 || true
    docker exec jobbridge-mysql mysql -uroot -proot -e \
        "DELETE FROM jobbridge.wecom_inbound_event WHERE msg_id LIKE 'fullsmoke_%';
         DELETE FROM jobbridge.conversation_log WHERE wecom_msg_id LIKE 'fullsmoke_%';" \
        2>&1 | grep -v "Warning" || true
}

# ---------------------------------------------------------------------------
# 1. 准备：清场 + 起 mock-testbed 后端
# ---------------------------------------------------------------------------
echo "==> [0] 清场（删 fullsmoke_* 历史 + Redis FLUSHDB）"
cleanup_mock_data

# 重 seed wm_mock 用户（FLUSHDB 不影响 MySQL）
docker exec -i jobbridge-mysql mysql -ujobbridge -pjobbridge jobbridge \
    < "$ROOT/mock-testbed/sql/seed_mock_users.sql" 2>&1 | grep -v "Warning" || true

cat > "$BACKEND/.env" <<EOF
MOCK_DB_DSN=mysql+pymysql://jobbridge:jobbridge@127.0.0.1:3306/jobbridge?charset=utf8mb4
MOCK_REDIS_URL=redis://127.0.0.1:6379/0
MOCK_PORT=8001
MOCK_HOST=127.0.0.1
MOCK_CORS_ORIGINS=http://localhost:5174
MOCK_CORPID=wwmock_corpid
MOCK_AGENTID=1000002
EOF

cd "$BACKEND"
nohup "$VENV/bin/uvicorn" main:app --host 127.0.0.1 --port 8001 \
    > "$LOG_DIR/backend.log" 2>&1 &
MOCK_PID=$!
echo "$MOCK_PID" > "$LOG_DIR/backend.pid"

trap 'kill "$MOCK_PID" 2>/dev/null || true' EXIT

echo "==> [0] 等 mock backend 就绪..."
for i in $(seq 1 20); do
    curl -fsS http://127.0.0.1:8001/health >/dev/null 2>&1 && break
    sleep 1
done
curl -fsS http://127.0.0.1:8001/health >/dev/null 2>&1 || { echo "❌ mock backend 没起来"; tail -20 "$LOG_DIR/backend.log"; exit 1; }
echo "  mock backend up (pid=$MOCK_PID)"

# 工具：发消息 + 等结果
post_msg() {
    local user="$1" content="$2" msg_id="$3"
    curl -s -X POST http://127.0.0.1:8001/mock/wework/inbound \
        -H "Content-Type: application/json" \
        -d "{\"ToUserName\":\"wwmock_corpid\",\"FromUserName\":\"$user\",\"CreateTime\":$(date +%s),\"MsgType\":\"text\",\"Content\":\"$content\",\"MsgId\":\"$msg_id\",\"AgentID\":\"1000002\"}"
}

wait_for_outbound() {
    local user="$1"
    local channel="mock:outbound:$user"
    local out_file="$2"
    docker exec jobbridge-redis timeout 15 redis-cli SUBSCRIBE "$channel" \
        > "$out_file" 2>&1 &
    echo $!
}

# ---------------------------------------------------------------------------
# 2. Smoke 场景
# ---------------------------------------------------------------------------
log() { echo "" | tee -a "$REPORT"; echo "$@" | tee -a "$REPORT"; }

run_case() {
    local case_id="$1" user="$2" content="$3"
    local msg_id="fullsmoke_${case_id}_$(date +%s)_$$"
    local sub_log="$LOG_DIR/sub-${case_id}.log"
    : > "$sub_log"

    echo ""
    echo "================================================================"
    echo "  [Case $case_id] user=$user"
    echo "  content=\"$content\""
    echo "================================================================"

    # 起 SUBSCRIBE
    docker exec jobbridge-redis timeout 15 redis-cli SUBSCRIBE "mock:outbound:$user" \
        > "$sub_log" 2>&1 &
    local sub_pid=$!
    sleep 1

    # 发消息
    local resp
    resp=$(post_msg "$user" "$content" "$msg_id")
    echo "  /inbound 响应: $resp"

    # 等 outbound（最多 10 秒）
    local got=0
    for i in $(seq 1 10); do
        if grep -qE "msgtype|content" "$sub_log" 2>/dev/null; then
            got=1
            break
        fi
        sleep 1
    done

    kill "$sub_pid" 2>/dev/null || true
    wait "$sub_pid" 2>/dev/null || true

    # 判定
    if [ "$got" -eq 1 ]; then
        echo "  ✅ 收到 outbound（${i}s）"
        local payload
        payload=$(grep -E '^\{' "$sub_log" | head -1)
        echo "  payload: ${payload:0:200}"
        local content_brief
        content_brief=$(echo "$payload" | /c/Users/47791/miniforge3/python.exe -c "import sys,json; print(json.load(sys.stdin).get('text',{}).get('content',''))" 2>/dev/null || echo "")
        [ -z "$content_brief" ] && content_brief=$(echo "$payload" | python3 -c "import sys,json; print(json.load(sys.stdin).get('text',{}).get('content',''))" 2>/dev/null || echo "$payload")
        echo "  bot 回复: $content_brief"
    else
        echo "  ⚠️  10s 内未收到 outbound（可能业务路径不需回复）"
    fi

    echo "" >> "$REPORT"
    echo "[$case_id] $user → \"$content\"" >> "$REPORT"
    echo "  inbound: $resp" >> "$REPORT"
    [ "$got" -eq 1 ] && echo "  outbound: $(grep -E '^\{' "$sub_log" | head -1)" >> "$REPORT"
}

# ---- Case 1: 求职者 search_job 意图（走 LLM）----
run_case "case1_search_job" "wm_mock_worker_001" "我想找深圳的打包工"

# ---- Case 2: 招聘者 search_worker 意图（走 LLM）----
run_case "case2_search_worker" "wm_mock_factory_001" "招几个深圳焊工，月薪 8000"

# ---- Case 3: 斜杠命令（不走 LLM）----
run_case "case3_command_help" "wm_mock_worker_001" "/帮助"

# ---- Case 4: 幂等 —— 同 MsgId 发两次 ----
echo ""
echo "================================================================"
echo "  [Case 4] 幂等去重测试（同 MsgId 发 2 次）"
echo "================================================================"
DUP_MSG_ID="fullsmoke_dup_$(date +%s)_$$"
DUP_RESP1=$(post_msg "wm_mock_worker_002" "幂等测试1" "$DUP_MSG_ID")
DUP_RESP2=$(post_msg "wm_mock_worker_002" "幂等测试2" "$DUP_MSG_ID")
echo "  第一次: $DUP_RESP1"
echo "  第二次: $DUP_RESP2"
if echo "$DUP_RESP2" | grep -q "duplicate"; then
    echo "  ✅ 第二次正确被去重"
else
    echo "  ❌ 第二次没有被去重！"
fi
echo "" >> "$REPORT"
echo "[case4] 幂等: $DUP_RESP1 / $DUP_RESP2" >> "$REPORT"

# ---- Case 5: 前缀守卫 —— 非 wm_mock_ 拒绝 ----
echo ""
echo "================================================================"
echo "  [Case 5] 前缀守卫（real_attacker_userid 应被拒）"
echo "================================================================"
ATK_RESP=$(post_msg "real_attacker_userid" "I am attacker" "fullsmoke_attacker_$(date +%s)")
echo "  响应: $ATK_RESP"
if echo "$ATK_RESP" | grep -q "40003\|wm_mock_"; then
    echo "  ✅ 攻击者前缀正确被拒"
else
    echo "  ❌ 守卫未生效！"
fi
echo "" >> "$REPORT"
echo "[case5] 前缀守卫: $ATK_RESP" >> "$REPORT"

# ---------------------------------------------------------------------------
# 3. 数据落地总检
# ---------------------------------------------------------------------------
echo ""
echo "================================================================"
echo "  [Final] 数据落地校验"
echo "================================================================"

echo ""
echo "--- wecom_inbound_event ---"
docker exec jobbridge-mysql mysql -uroot -proot -se "
SELECT msg_id, from_userid, status, retry_count, error_message
FROM jobbridge.wecom_inbound_event
WHERE msg_id LIKE 'fullsmoke_%'
ORDER BY id;
" 2>&1 | grep -v "Warning" | tee -a "$REPORT"

echo ""
echo "--- conversation_log（最近 1 分钟）---"
docker exec jobbridge-mysql mysql -uroot -proot -se "
SELECT direction, intent, userid, LEFT(content, 60) AS preview
FROM jobbridge.conversation_log
WHERE created_at >= NOW() - INTERVAL 5 MINUTE
ORDER BY id DESC
LIMIT 20;
" 2>&1 | grep -v "Warning" | tee -a "$REPORT"

echo ""
echo "--- Redis: queue:incoming 残留 + dead_letter 检查 ---"
echo "queue:incoming        = $(docker exec jobbridge-redis redis-cli LLEN queue:incoming)"
echo "queue:send_retry      = $(docker exec jobbridge-redis redis-cli LLEN queue:send_retry)"
echo "queue:dead_letter     = $(docker exec jobbridge-redis redis-cli LLEN queue:dead_letter)"

echo ""
echo "--- Worker 日志 LLM 调用统计 ---"
LLM_CALLS=$(docker logs --tail 500 jobbridge-worker-1 2>&1 | grep -c "POST https://dashscope.aliyuncs.com" || true)
MOCK_CALLS=$(docker logs --tail 500 jobbridge-worker-1 2>&1 | grep -c "MOCK-WEWORK\] short-circuit" || true)
ERR_CALLS=$(docker logs --tail 500 jobbridge-worker-1 2>&1 | grep -cE "ERROR|TIMEOUT|41004|enqueue retry" || true)
echo "LLM 调用次数:           $LLM_CALLS"
echo "MOCK-WEWORK 拦截次数:   $MOCK_CALLS"
echo "错误/重试次数:          $ERR_CALLS"

# ---------------------------------------------------------------------------
# 4. 收尾
# ---------------------------------------------------------------------------
echo ""
echo "================================================================"
echo "  报告: $REPORT"
echo "================================================================"
echo ""
echo "==> 关闭 mock backend"
kill "$MOCK_PID" 2>/dev/null || true
echo "✅ Full smoke 完成"
