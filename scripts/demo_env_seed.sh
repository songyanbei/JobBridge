#!/usr/bin/env bash
# Phase 4 Demo seed 数据生成 + 执行脚本
# 用法：
#   FACTORY_ZZ=<厂家张总 userid> \
#   FACTORY_LZ=<厂家李总 userid> \
#   BROKER_LJ=<中介李姐 userid> \
#     bash scripts/demo_env_seed.sh
#
# 作用：
#   1. 将 seed_demo.sql.template 的占位符替换为真实 userid，输出 seed_demo.sql
#   2. 执行到 MySQL

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$PROJECT_ROOT/backend/sql/seed_demo.sql.template"
OUTPUT="$PROJECT_ROOT/backend/sql/seed_demo.sql"

# 校验环境变量
for var in FACTORY_ZZ FACTORY_LZ BROKER_LJ; do
    if [ -z "${!var:-}" ]; then
        echo "[x] 缺少环境变量 $var"
        echo ""
        echo "用法示例："
        echo "  FACTORY_ZZ=ZhangZhiQiang \\"
        echo "  FACTORY_LZ=LiJianJun \\"
        echo "  BROKER_LJ=LiGuiFang \\"
        echo "    bash scripts/demo_env_seed.sh"
        exit 1
    fi
done

if [ ! -f "$TEMPLATE" ]; then
    echo "[x] 找不到模板 $TEMPLATE"
    exit 1
fi

echo "[+] 生成 seed_demo.sql..."
sed \
    -e "s|__FACTORY_ZZ_USERID__|$FACTORY_ZZ|g" \
    -e "s|__FACTORY_LZ_USERID__|$FACTORY_LZ|g" \
    -e "s|__BROKER_LJ_USERID__|$BROKER_LJ|g" \
    "$TEMPLATE" > "$OUTPUT"

echo "[+] 执行 seed 到 MySQL..."
if ! docker ps --format '{{.Names}}' | grep -q jobbridge-mysql; then
    echo "[x] jobbridge-mysql 未运行"
    exit 1
fi

docker exec -i jobbridge-mysql \
    mysql -u jobbridge -pjobbridge jobbridge < "$OUTPUT"

echo ""
echo "[✓] Seed 完成"
echo ""
echo "验证："
docker exec jobbridge-mysql mysql -u jobbridge -pjobbridge jobbridge -e "
SELECT role, COUNT(*) AS cnt FROM user
  WHERE external_userid IN ('$FACTORY_ZZ','$FACTORY_LZ','$BROKER_LJ')
  GROUP BY role;
SELECT COUNT(*) AS total_jobs FROM job
  WHERE owner_userid IN ('$FACTORY_ZZ','$FACTORY_LZ','$BROKER_LJ');
" 2>/dev/null | grep -v Warning
