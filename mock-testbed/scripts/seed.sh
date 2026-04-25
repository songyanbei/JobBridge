#!/usr/bin/env bash
# Mock 企业微信测试台 · DB seed 脚本
#
# 把 4 个 wm_mock_* 用户幂等 INSERT 到主库的 user 表。
# 读取 mock-testbed/backend/.env 中的 MOCK_DB_DSN 来解析连接信息。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SQL_FILE="$ROOT_DIR/sql/seed_mock_users.sql"
ENV_FILE="$ROOT_DIR/backend/.env"

if [ ! -f "$SQL_FILE" ]; then
    echo "❌ seed SQL 不存在：$SQL_FILE"
    exit 1
fi

if [ ! -f "$ENV_FILE" ]; then
    echo "⚠️  未找到 $ENV_FILE，使用 .env.example 作 fallback"
    ENV_FILE="$ROOT_DIR/backend/.env.example"
fi

# 解析 MOCK_DB_DSN，格式形如 mysql+pymysql://user:pass@host:port/dbname
DSN=$(grep -E '^MOCK_DB_DSN=' "$ENV_FILE" | head -1 | cut -d= -f2-)
if [ -z "$DSN" ]; then
    echo "❌ $ENV_FILE 中找不到 MOCK_DB_DSN"
    exit 1
fi

# 用 Python 解析 DSN 避免 shell 正则的痛
if command -v python >/dev/null 2>&1; then
    PY=python
elif command -v python3 >/dev/null 2>&1; then
    PY=python3
else
    echo "❌ 找不到 python，无法解析 DSN"
    exit 1
fi

IFS='|' read -r DB_USER DB_PASS DB_HOST DB_PORT DB_NAME <<EOF
$("$PY" - "$DSN" <<'PYEOF'
import sys
from urllib.parse import urlparse, unquote

dsn = sys.argv[1]
# 去掉 dialect+driver 前缀
if '://' in dsn:
    schema, rest = dsn.split('://', 1)
    dsn = 'mysql://' + rest
u = urlparse(dsn)
user = unquote(u.username or 'root')
pwd = unquote(u.password or '')
host = u.hostname or 'localhost'
port = u.port or 3306
name = (u.path or '/').lstrip('/')
print(f'{user}|{pwd}|{host}|{port}|{name}')
PYEOF
)
EOF

echo "▶ 连接：${DB_USER}@${DB_HOST}:${DB_PORT}/${DB_NAME}"
echo "▶ 灌入：$SQL_FILE"
echo ""

if [ -z "${DB_PASS}" ]; then
    mysql -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" "$DB_NAME" < "$SQL_FILE"
else
    MYSQL_PWD="$DB_PASS" mysql -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" "$DB_NAME" < "$SQL_FILE"
fi

echo ""
echo "✅ seed 完成。验证："
if [ -z "${DB_PASS}" ]; then
    mysql -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" "$DB_NAME" \
        -e "SELECT external_userid, role, display_name FROM user WHERE external_userid LIKE 'wm_mock_%' ORDER BY role, external_userid;"
else
    MYSQL_PWD="$DB_PASS" mysql -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" "$DB_NAME" \
        -e "SELECT external_userid, role, display_name FROM user WHERE external_userid LIKE 'wm_mock_%' ORDER BY role, external_userid;"
fi
