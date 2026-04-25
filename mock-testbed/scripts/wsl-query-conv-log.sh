#!/usr/bin/env bash
set -e
TOKEN=$(curl -s -X POST http://localhost:8000/admin/login \
    -H "Content-Type: application/json" \
    -d '{"username":"admin","password":"DemoPass2026!"}' \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('data',{}).get('access_token',''))")

START=$(date -u -d "1 hour ago" +%Y-%m-%dT%H:%M:%S)
END=$(date -u +%Y-%m-%dT%H:%M:%S)
echo "查询窗口: $START → $END (UTC)"
echo ""

for user in wm_mock_worker_001 wm_mock_worker_002 wm_mock_factory_001; do
    echo "=== userid=$user ==="
    curl -s "http://localhost:8000/admin/logs/conversations?userid=$user&start=$START&end=$END" \
        -H "Authorization: Bearer $TOKEN" \
        -H "Accept: application/json" \
        | python3 <<PYEOF
import sys, json
d = json.load(sys.stdin)
items = d.get('data', {}).get('items', [])
total = d.get('data', {}).get('total', '?')
print(f"  total={total}, 取回 {len(items)} 条")
for i in items[:6]:
    direction = i.get('direction','-')
    intent = i.get('intent') or '-'
    content = (i.get('content') or '')[:70]
    ts = (i.get('created_at') or '')[:19]
    print(f"    [{ts}] {direction:3} intent={intent:12} content={content}")
PYEOF
    echo ""
done
