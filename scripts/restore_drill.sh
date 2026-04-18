#!/usr/bin/env bash
# ============================================================================
# 禁止在生产环境执行 —— 仅用于预发演练的数据库恢复参考脚本
# ============================================================================
# Phase 7 §3.1 模块 K：每次发布前在预发环境跑一次恢复演练，把演练结果
# （耗时 / 条数 / 异常）写入 collaboration/handoffs/phase7-release-report.md。
#
# 用法：
#   ./restore_drill.sh <dump.sql.gz> <target_mysql_container>
#
# 例：
#   ./restore_drill.sh /data/backup/mysql/jobbridge_20260418_033000.sql.gz \
#       jobbridge-mysql-staging
#
# 环境变量：
#   MYSQL_ROOT_PASSWORD   target 容器的 MySQL root 密码（必填）
#   DB_NAME               目标数据库名（默认 jobbridge）
# ============================================================================
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 <dump.sql.gz> <target_mysql_container>" >&2
  exit 1
fi

DUMP_FILE=$1
TARGET=$2
DB_NAME=${DB_NAME:-jobbridge}

if [[ -z "${MYSQL_ROOT_PASSWORD:-}" ]]; then
  echo "[restore_drill] FATAL: MYSQL_ROOT_PASSWORD is empty" >&2
  exit 2
fi

# 防呆：明确确认
read -r -p "⚠️ You are about to IMPORT $DUMP_FILE into container $TARGET / db $DB_NAME. Continue? [yes/NO] " ans
if [[ "$ans" != "yes" ]]; then
  echo "aborted."
  exit 0
fi

echo "[restore_drill] target=$TARGET db=$DB_NAME dump=$DUMP_FILE"
START=$(date +%s)

# 为安全起见先 DROP/CREATE
docker exec -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" "$TARGET" \
  mysql -u root -e "DROP DATABASE IF EXISTS \`$DB_NAME\`; CREATE DATABASE \`$DB_NAME\` DEFAULT CHARSET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"

zcat "$DUMP_FILE" | docker exec -i -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" "$TARGET" \
  mysql -u root "$DB_NAME"

END=$(date +%s)
echo "[restore_drill] done in $((END - START))s"

# 简单核验
docker exec -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" "$TARGET" \
  mysql -u root -e "USE \`$DB_NAME\`; SELECT
    (SELECT COUNT(*) FROM user)                 AS user_count,
    (SELECT COUNT(*) FROM job)                  AS job_count,
    (SELECT COUNT(*) FROM resume)               AS resume_count,
    (SELECT COUNT(*) FROM conversation_log)     AS conv_count,
    (SELECT COUNT(*) FROM audit_log)            AS audit_count,
    (SELECT COUNT(*) FROM wecom_inbound_event)  AS inbound_count;"

echo "[restore_drill] please append results to phase7-release-report.md"
