#!/usr/bin/env bash
# 在 WSL 里 build 主前端 admin SPA
# 思路：Windows mount 上的 npm install 慢得离谱，改放 /tmp 做 install + build，
# 再把 dist 同步回 frontend/dist 让 nginx 容器能挂到
set -e

ROOT=/mnt/d/work/JobBridge/.claude/worktrees/romantic-proskuriakova-bdc75a
FE_SRC="$ROOT/frontend"
TMP_FE=/tmp/jb-admin-frontend

echo "==> 同步前端源码到 WSL-local /tmp（首次复制 ~30s）"
mkdir -p "$TMP_FE"
# 只同步源码不带 dist/node_modules
rsync -a --delete \
    --exclude node_modules --exclude dist --exclude .vite \
    "$FE_SRC"/ "$TMP_FE"/

cd "$TMP_FE"

if [ ! -d node_modules ]; then
    echo "==> npm install（首次 1-3 分钟）..."
    npm install --silent 2>&1 | tail -5
fi

echo "==> npm run build"
npm run build 2>&1 | tail -10

# 同步 dist 回去
echo "==> 同步 dist → $FE_SRC/dist"
rsync -a --delete "$TMP_FE/dist/" "$FE_SRC/dist/"

echo ""
echo "=== 验证 dist ==="
ls -la "$FE_SRC/dist/" | head -8
echo ""
echo "=== nginx 容器内能看到吗 ==="
docker exec jobbridge-nginx sh -c 'ls /usr/share/nginx/html/admin/ | head -10'
echo ""
echo "==> 测试 /admin/login"
curl -s -o /dev/null -w "HTTP %{http_code}, content-length=%{size_download}\n" \
    -H "Accept: text/html" http://localhost:8000/admin/login
