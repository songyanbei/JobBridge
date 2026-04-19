#!/usr/bin/env bash
# ============================================================================
# fix_mojibake.sh  —  修复 seed 双重编码导致的中文乱码
#
# 典型症状：列表里的 "王大海" 显示为 "çŽ‹å¤§æµ·"
# 成因：早期 demo_env_seed.sh 通过 mysql client 默认 latin1 charset 灌 UTF-8
#       SQL 文件，每个 CJK 字符的 3 字节被当作 3 个 latin1 字符再存为 utf8mb4。
#
# 用法：
#   bash scripts/fix_mojibake.sh           # 先 dry-run（只 SELECT 不改）
#   bash scripts/fix_mojibake.sh --apply   # 确认后执行 UPDATE
#
# 前置条件：docker 容器 jobbridge-mysql 正在跑。
# ============================================================================

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SQL_FILE="$PROJECT_ROOT/backend/sql/fix_mojibake.sql"
MODE="${1:-dry-run}"

if ! docker ps --format '{{.Names}}' | grep -q jobbridge-mysql; then
    echo "[x] jobbridge-mysql 容器未运行"
    exit 1
fi

MYSQL_CMD="docker exec -i jobbridge-mysql mysql --default-character-set=utf8mb4 -u jobbridge -pjobbridge jobbridge"

echo "[+] 预览：user 表中受影响的行（最多 20 条）"
echo "    左侧是当前存的 mojibake，右侧是反 decode 后的正确值。"
echo ""

$MYSQL_CMD <<'SQL' 2>/dev/null | grep -v '^mysql:'
SELECT
  external_userid,
  display_name AS current_value,
  CONVERT(CAST(CONVERT(display_name USING latin1) AS BINARY) USING utf8mb4) AS fixed
FROM `user`
WHERE display_name IS NOT NULL
  AND (display_name LIKE '%ç%' OR display_name LIKE '%å%' OR display_name LIKE '%æ%'
       OR display_name LIKE '%è%' OR display_name LIKE '%é%' OR display_name LIKE '%ã%')
LIMIT 20;
SQL

echo ""

if [ "$MODE" != "--apply" ]; then
    echo "[ ] Dry-run 结束。如果右侧 'fixed' 列看起来是正确的中文，执行："
    echo "      bash scripts/fix_mojibake.sh --apply"
    echo ""
    echo "    如果 fixed 列还是乱或为空，说明不是这种 mojibake 模式，请先停下来排查。"
    exit 0
fi

echo "[+] 即将对 user / job / resume / conversation_log / audit_log 中的"
echo "    文本列执行反 decode UPDATE。脚本包含事务，最后 COMMIT。"
read -rp "继续? [y/N] " go
[[ "${go,,}" != "y" ]] && { echo "中止"; exit 0; }

echo ""
echo "[+] 先备份相关表到 /tmp/jobbridge_mojibake_backup_$(date +%s).sql ..."
BACKUP_FILE="/tmp/jobbridge_mojibake_backup_$(date +%s).sql"
docker exec jobbridge-mysql mysqldump \
    --default-character-set=utf8mb4 \
    -u jobbridge -pjobbridge jobbridge \
    user job resume conversation_log audit_log > "$BACKUP_FILE" 2>/dev/null
echo "    备份到：$BACKUP_FILE ($(wc -c <"$BACKUP_FILE") bytes)"
echo ""

echo "[+] 执行 fix_mojibake.sql ..."
$MYSQL_CMD < "$SQL_FILE"

echo ""
echo "[✓] 完成。刷新前端页面看一下 display_name / company 是否正确。"
echo "    如需回滚：docker exec -i jobbridge-mysql mysql --default-character-set=utf8mb4 \\"
echo "              -u jobbridge -pjobbridge jobbridge < $BACKUP_FILE"
