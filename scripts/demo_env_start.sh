#!/usr/bin/env bash
# Phase 4 Demo 环境一键启动脚本
# 用法：bash scripts/demo_env_start.sh
#
# 启动 3 个后台进程（在 tmux 会话 jobbridge-demo 下）：
#   - cpolar：公网 HTTPS 隧道
#   - uvicorn：FastAPI（webhook）
#   - worker：异步消息 Worker
#
# 前置条件：
#   - WSL2 / Linux + tmux
#   - backend/.venv-wsl 已建好
#   - Docker 的 jobbridge-mysql / jobbridge-redis 已 healthy
#   - backend/.env 已填入真实的 WECOM_* 和 cpolar 子域名
#   - 环境变量 CPOLAR_SUBDOMAIN 已导出，或 .env 里配置 DEMO_CPOLAR_SUBDOMAIN

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SESSION="jobbridge-demo"

cd "$PROJECT_ROOT"

# 加载 .env 读取 DEMO_CPOLAR_SUBDOMAIN
if [ -f backend/.env ]; then
    # shellcheck disable=SC1091
    set -a
    source <(grep -E '^(DEMO_CPOLAR_SUBDOMAIN|CPOLAR_AUTH_TOKEN)=' backend/.env || true)
    set +a
fi

CPOLAR_SUBDOMAIN="${CPOLAR_SUBDOMAIN:-${DEMO_CPOLAR_SUBDOMAIN:-}}"

if [ -z "$CPOLAR_SUBDOMAIN" ]; then
    echo "[x] 请先在 backend/.env 里设置 DEMO_CPOLAR_SUBDOMAIN，"
    echo "    或执行前导出：export CPOLAR_SUBDOMAIN=xxx"
    exit 1
fi

# 检查 docker 容器
if ! docker ps --format '{{.Names}}' | grep -q jobbridge-mysql; then
    echo "[!] jobbridge-mysql 未运行，尝试启动..."
    docker start jobbridge-mysql jobbridge-redis || true
    sleep 5
fi

# 检查 cpolar 是否已安装
if ! command -v cpolar >/dev/null 2>&1; then
    echo "[x] cpolar 未安装，请先执行："
    echo "    curl -L https://www.cpolar.com/static/downloads/install-release-cpolar.sh | sudo bash"
    echo "    sudo cpolar authtoken <your-authtoken>"
    exit 1
fi

# 如果 tmux 会话已存在，先清掉
if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "[i] 旧的 tmux 会话 $SESSION 已存在，先清理..."
    tmux kill-session -t "$SESSION"
fi

echo "[+] 创建 tmux 会话 $SESSION..."

tmux new-session -d -s "$SESSION" -n cpolar \
    "cd $PROJECT_ROOT && cpolar http --region cn_top --subdomain $CPOLAR_SUBDOMAIN 8000 2>&1 | tee /tmp/cpolar.log"

tmux new-window -t "$SESSION" -n uvicorn \
    "cd $PROJECT_ROOT/backend && source .venv-wsl/bin/activate && \
     uvicorn app.main:app --host 0.0.0.0 --port 8000 --log-level info 2>&1 | tee /tmp/uvicorn.log"

tmux new-window -t "$SESSION" -n worker \
    "cd $PROJECT_ROOT/backend && source .venv-wsl/bin/activate && \
     python -m app.services.worker 2>&1 | tee /tmp/worker.log"

tmux new-window -t "$SESSION" -n logs \
    "tail -f /tmp/cpolar.log /tmp/uvicorn.log /tmp/worker.log"

sleep 3

echo ""
echo "=========================================="
echo "[✓] Demo 环境已启动"
echo ""
echo "  公网回调地址：https://$CPOLAR_SUBDOMAIN.cpolar.top/webhook/wecom"
echo "  健康检查：    https://$CPOLAR_SUBDOMAIN.cpolar.top/health"
echo ""
echo "  看实时日志：  tmux attach -t $SESSION"
echo "  切换窗口：    Ctrl-b 然后按 0/1/2/3"
echo "  后台退出：    Ctrl-b 然后按 d"
echo ""
echo "  停止：        bash scripts/demo_env_stop.sh"
echo "=========================================="
