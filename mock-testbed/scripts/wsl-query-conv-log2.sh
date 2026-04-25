#!/usr/bin/env bash
TOKEN=$(curl -s -X POST http://localhost:8000/admin/login \
    -H "Content-Type: application/json" \
    -d '{"username":"admin","password":"DemoPass2026!"}' \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('data',{}).get('access_token',''))")
echo "token len=${#TOKEN}"

TZ=Asia/Shanghai
START=$(TZ="$TZ" date -d "1 hour ago" +%Y-%m-%dT%H:%M:%S)
END=$(TZ="$TZ" date +%Y-%m-%dT%H:%M:%S)
echo "窗口: $START → $END"

URL="http://localhost:8000/admin/logs/conversations?userid=wm_mock_worker_001&start=${START}&end=${END}"
echo "URL: $URL"

OUT=$(mktemp)
curl -s -w "\nHTTP_CODE=%{http_code}\n" "$URL" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Accept: application/json" > "$OUT"
echo ""
echo "=== 响应 ==="
cat "$OUT"
rm -f "$OUT"
