#!/usr/bin/env bash
# Mock 企业微信测试台 · 冒烟脚本
#
# 验证后端 5 个路由 + health + 根路径都能响应 200。
# 不测真实 Worker 消费链路（那需要主后端也在跑）。
set -uo pipefail

MOCK_PORT="${MOCK_PORT:-8001}"
BASE="http://localhost:$MOCK_PORT"

PASS=0
FAIL=0

run_check() {
    local name="$1"
    local expected_code="$2"
    shift 2
    # 其余参数是 curl 的参数
    local code
    code=$(curl -s -o /dev/null -w "%{http_code}" "$@" 2>/dev/null || echo "000")
    if [ "$code" = "$expected_code" ]; then
        echo "  ✅ $name → $code"
        PASS=$((PASS + 1))
    else
        echo "  ❌ $name → 期望 $expected_code，实际 $code"
        FAIL=$((FAIL + 1))
    fi
}

echo "============================================================"
echo "  Mock 测试台 · 冒烟（后端 $BASE）"
echo "============================================================"

run_check "GET  /health"                              "200" -X GET  "$BASE/health"
run_check "GET  /"                                    "200" -X GET  "$BASE/"
run_check "GET  /mock/wework/users"                   "200" -X GET  "$BASE/mock/wework/users"
run_check "GET  /mock/wework/oauth2/authorize (302)"  "302" -X GET  --max-redirs 0 \
    "$BASE/mock/wework/oauth2/authorize?appid=wwX&redirect_uri=http%3A%2F%2Fexample.com%2Fcb&state=abc"
run_check "GET  /mock/wework/code2userinfo"           "200" -X GET  "$BASE/mock/wework/code2userinfo?access_token=x&code=y"

# POST inbound 构造一个完整 payload
MSG_ID="smoke_$(date +%s)_$$"
PAYLOAD=$(cat <<JSON
{"ToUserName":"wwmock_corpid","FromUserName":"wm_mock_worker_001","CreateTime":$(date +%s),"MsgType":"text","Content":"smoke test","MsgId":"$MSG_ID","AgentID":"1000002"}
JSON
)
run_check "POST /mock/wework/inbound"                 "200" -X POST \
    -H "Content-Type: application/json" -d "$PAYLOAD" "$BASE/mock/wework/inbound"

# SSE 端点 —— 只检查 200 和 Content-Type，不消费消息
echo "  ▶ GET  /mock/wework/sse 抽检 header..."
SSE_HEADERS=$(curl -s -o /dev/null --max-time 2 -D - \
    "$BASE/mock/wework/sse?external_userid=wm_mock_worker_001" 2>/dev/null || true)
if echo "$SSE_HEADERS" | grep -qi "text/event-stream"; then
    echo "  ✅ SSE Content-Type 包含 text/event-stream"
    PASS=$((PASS + 1))
else
    echo "  ❌ SSE 未返回 event-stream 头"
    FAIL=$((FAIL + 1))
fi

echo ""
echo "============================================================"
echo "  结果：PASS=$PASS FAIL=$FAIL"
echo "============================================================"

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
