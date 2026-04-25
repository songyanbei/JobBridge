#!/usr/bin/env bash
echo "=== nginx 容器静态文件目录 ==="
docker exec jobbridge-nginx ls -la /usr/share/nginx/html/ 2>&1 | head -10
echo ""
echo "=== /admin/ 静态目录 ==="
docker exec jobbridge-nginx ls -la /usr/share/nginx/html/admin/ 2>&1 | head -15
echo ""
echo "=== curl /admin/login 模拟浏览器 ==="
curl -s -I -H "Accept: text/html,application/xhtml+xml" http://localhost:8000/admin/login 2>&1 | head -8
echo ""
echo "=== curl /admin/audit 模拟浏览器 ==="
curl -s -I -H "Accept: text/html,application/xhtml+xml" http://localhost:8000/admin/audit 2>&1 | head -8
echo ""
echo "=== nginx error log ==="
docker exec jobbridge-nginx tail -15 /var/log/nginx/error.log 2>&1 | tail -15
echo ""
echo "=== nginx access log（admin 相关，最近 10 条）==="
docker logs --tail 50 jobbridge-nginx 2>&1 | grep "/admin" | tail -10
