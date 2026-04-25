#!/usr/bin/env bash
echo "=== /usr/share/nginx/html ==="
docker exec jobbridge-nginx sh -c 'ls -la /usr/share/nginx/html/'
echo ""
echo "=== /usr/share/nginx/html/admin ==="
docker exec jobbridge-nginx sh -c 'ls /usr/share/nginx/html/admin/' 2>&1
echo ""
echo "=== /admin/login HTTP head ==="
curl -sI -H 'Accept: text/html' http://localhost:8000/admin/login | head -10
echo ""
echo "=== /admin/audit HTTP head ==="
curl -sI -H 'Accept: text/html' http://localhost:8000/admin/audit | head -10
echo ""
echo "=== nginx error log ==="
docker exec jobbridge-nginx sh -c 'tail -15 /var/log/nginx/error.log'
echo ""
echo "=== nginx access log（admin 相关）==="
docker logs --tail 50 jobbridge-nginx 2>&1 | grep '/admin' | tail -8
