#!/usr/bin/env bash
set -e
TOKEN=$(curl -s -X POST http://localhost:8000/admin/login \
    -H "Content-Type: application/json" \
    -d '{"username":"admin","password":"DemoPass2026!"}' \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('data',{}).get('access_token',''))")
echo "token len=${#TOKEN}"
echo ""

for path in \
    "/admin/audit/pending-count" \
    "/admin/audit/queue?status=pending&page=1&size=5" \
    "/admin/accounts/factories?page=1&size=5"
do
    echo "=== GET $path ==="
    curl -s -w "\nHTTP %{http_code}\n" "http://localhost:8000$path" \
        -H "Authorization: Bearer $TOKEN" | head -30
    echo "---"
done
