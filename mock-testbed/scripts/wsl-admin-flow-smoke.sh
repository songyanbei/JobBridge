#!/usr/bin/env bash
# 管理后台审核流程冒烟：
#   登录 → 强制改密 → 注入 pending Job/Resume → /admin/audit/queue 列表
#   → /admin/audit/pass (Job) → /admin/audit/reject (Resume) → 验证状态 → 清理
set -euo pipefail

ADMIN_BASE="http://localhost:8000"
ADMIN_USER=admin
ADMIN_OLD_PASS=admin123
ADMIN_NEW_PASS=DemoPass2026!
LOG_DIR=/tmp/mock-testbed-stress-logs
mkdir -p "$LOG_DIR"

# 工具
js() { python3 -c "import sys,json; d=json.load(open('$1')); $2"; }
jq_field() { python3 -c "import sys,json; d=json.load(open('$1')); print(d$2)" 2>/dev/null || true; }

SMOKE_TAG='__SMOKE_FLOW_TAG__'
cleanup() {
    echo ""
    echo "==> 清理测试数据"
    docker exec jobbridge-mysql mysql -uroot -proot -e "
        DELETE FROM jobbridge.audit_log WHERE target_id IN (
            SELECT id FROM jobbridge.job WHERE raw_text LIKE '%${SMOKE_TAG}%'
            UNION
            SELECT id FROM jobbridge.resume WHERE raw_text LIKE '%${SMOKE_TAG}%'
        );
        DELETE FROM jobbridge.job WHERE raw_text LIKE '%${SMOKE_TAG}%';
        DELETE FROM jobbridge.resume WHERE raw_text LIKE '%${SMOKE_TAG}%';
    " 2>&1 | grep -vE "Warning|^$" || true
}
trap cleanup EXIT

# 工具：登录并返回 token
admin_login() {
    local pwd="$1" out="$2"
    curl -s -X POST "$ADMIN_BASE/admin/login" \
        -H "Content-Type: application/json" \
        -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$pwd\"}" \
        -o "$out"
}

# ============================================================================
# 1+2+3. 登录（兼容两种状态：新装 admin123 vs 已改密）
# ============================================================================
echo "==> [1] 登录"
admin_login "$ADMIN_OLD_PASS" "$LOG_DIR/login1.json"
LOGIN_CODE=$(python3 -c "import json; d=json.load(open('$LOG_DIR/login1.json')); print(d.get('code','?'))")

if [ "$LOGIN_CODE" = "0" ]; then
    TOKEN=$(python3 -c "import json; d=json.load(open('$LOG_DIR/login1.json')); print(d.get('data',{}).get('access_token',''))")
    PWD_CHANGED=$(python3 -c "import json; d=json.load(open('$LOG_DIR/login1.json')); print(d.get('data',{}).get('password_changed','?'))")
    echo "  admin/admin123 登录成功，password_changed=$PWD_CHANGED"

    if [ "$PWD_CHANGED" = "False" ]; then
        echo "==> [2] 首次登录 → 改密 → $ADMIN_NEW_PASS"
        curl -s -X PUT "$ADMIN_BASE/admin/me/password" \
            -H "Authorization: Bearer $TOKEN" \
    -H "Accept: application/json" \
            -H "Content-Type: application/json" \
            -d "{\"old_password\":\"$ADMIN_OLD_PASS\",\"new_password\":\"$ADMIN_NEW_PASS\"}" \
            -o "$LOG_DIR/pwd-change.json"
        PWD_CODE=$(python3 -c "import json; d=json.load(open('$LOG_DIR/pwd-change.json')); print(d.get('code','?'))")
        echo "  改密 code=$PWD_CODE"

        echo "==> [3] 新密码重登"
        admin_login "$ADMIN_NEW_PASS" "$LOG_DIR/login2.json"
        TOKEN=$(python3 -c "import json; d=json.load(open('$LOG_DIR/login2.json')); print(d.get('data',{}).get('access_token',''))")
    fi
else
    echo "  admin/admin123 失败（password_changed=True 残留），尝试新密码"
    admin_login "$ADMIN_NEW_PASS" "$LOG_DIR/login2.json"
    LOGIN2_CODE=$(python3 -c "import json; d=json.load(open('$LOG_DIR/login2.json')); print(d.get('code','?'))")
    if [ "$LOGIN2_CODE" != "0" ]; then
        echo "  ❌ 两个密码都不对"
        cat "$LOG_DIR/login1.json"
        cat "$LOG_DIR/login2.json"
        exit 1
    fi
    TOKEN=$(python3 -c "import json; d=json.load(open('$LOG_DIR/login2.json')); print(d.get('data',{}).get('access_token',''))")
    echo "  admin/$ADMIN_NEW_PASS 登录成功"
fi

[ -z "$TOKEN" ] && { echo "❌ 没拿到 token"; exit 1; }
echo "  token len=${#TOKEN}"

# ============================================================================
# 4. 直接 SQL 注入 pending Job + Resume（绕开多轮对话）
# ============================================================================
echo ""
echo "==> [4] SQL 注入 pending Job (factory) + Resume (worker)"

docker exec -i jobbridge-mysql mysql --default-character-set=utf8mb4 -uroot -proot jobbridge <<SQL_EOF 2>&1
SET NAMES utf8mb4;
INSERT INTO job (
  owner_userid, city, job_category, salary_floor_monthly, pay_type, headcount,
  gender_required, is_long_term, raw_text,
  audit_status, version, created_at, expires_at
)
VALUES (
  'wm_mock_factory_001', '深圳市', '焊工', 8000, '月薪', 5,
  '不限', 1, '${SMOKE_TAG} 招深圳焊工 5 人 月薪 8000',
  'pending', 1, NOW(), DATE_ADD(NOW(), INTERVAL 30 DAY)
);

INSERT INTO resume (
  owner_userid,
  expected_cities, expected_job_categories, salary_expect_floor_monthly,
  gender, age, accept_long_term, accept_short_term,
  raw_text,
  audit_status, version, created_at, expires_at
)
VALUES (
  'wm_mock_worker_001',
  JSON_ARRAY('深圳市'), JSON_ARRAY('焊工'), 8000,
  '男', 28, 1, 0,
  '${SMOKE_TAG} 28岁男 找深圳焊工',
  'pending', 1, NOW(), DATE_ADD(NOW(), INTERVAL 15 DAY)
);

SELECT '--- Job 注入 ---' AS step;
SELECT id, owner_userid, city, job_category, audit_status, version FROM job WHERE raw_text LIKE '%${SMOKE_TAG}%';
SELECT '--- Resume 注入 ---' AS step;
SELECT id, owner_userid, audit_status, version FROM resume WHERE raw_text LIKE '%${SMOKE_TAG}%';
SQL_EOF

# 取回 Job/Resume id
JOB_ID=$(docker exec jobbridge-mysql mysql -uroot -proot -se \
    "SELECT id FROM jobbridge.job WHERE raw_text LIKE '%${SMOKE_TAG}%' ORDER BY id DESC LIMIT 1;" 2>&1 | grep -vE "Warning|^$" | head -1)
RESUME_ID=$(docker exec jobbridge-mysql mysql -uroot -proot -se \
    "SELECT id FROM jobbridge.resume WHERE raw_text LIKE '%${SMOKE_TAG}%' ORDER BY id DESC LIMIT 1;" 2>&1 | grep -vE "Warning|^$" | head -1)
echo "  JOB_ID=$JOB_ID  RESUME_ID=$RESUME_ID"

# ============================================================================
# 5. /admin/audit/pending-count
# ============================================================================
echo ""
echo "==> [5] GET /admin/audit/pending-count"
curl -s "$ADMIN_BASE/admin/audit/pending-count" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Accept: application/json" \
    -o "$LOG_DIR/pending.json"
python3 -c "import json; d=json.load(open('$LOG_DIR/pending.json')); print('  ', json.dumps(d, ensure_ascii=False))"

# ============================================================================
# 6. /admin/audit/queue?status=pending
# ============================================================================
echo ""
echo "==> [6] GET /admin/audit/queue?status=pending"
curl -s "$ADMIN_BASE/admin/audit/queue?status=pending&page=1&size=50" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Accept: application/json" \
    -o "$LOG_DIR/queue.json"
python3 <<PYEOF
import json
d = json.load(open('$LOG_DIR/queue.json'))
print(f"  code={d.get('code','?')}, total={d.get('data',{}).get('total','?')}")
items = d.get('data',{}).get('items',[])
mock_items = [i for i in items if 'wm_mock_' in str(i.get('owner_userid',''))]
print(f"  共 {len(items)} 条，其中 wm_mock_* {len(mock_items)} 条")
for i in mock_items[:5]:
    print(f"    - target_type={i.get('target_type')} id={i.get('target_id')} owner={i.get('owner_userid')} brief={(i.get('brief') or '')[:50]}")
PYEOF

# ============================================================================
# 7. /admin/audit/job/{id}/pass
# ============================================================================
echo ""
echo "==> [7] POST /admin/audit/job/$JOB_ID/pass"
curl -s -X POST "$ADMIN_BASE/admin/audit/job/$JOB_ID/pass" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Accept: application/json" \
    -H "Content-Type: application/json" \
    -d "{\"version\":1}" \
    -o "$LOG_DIR/pass.json"
python3 -c "import json; d=json.load(open('$LOG_DIR/pass.json')); print('  ', json.dumps(d, ensure_ascii=False))"

# ============================================================================
# 8. /admin/audit/resume/{id}/reject
# ============================================================================
echo ""
echo "==> [8] POST /admin/audit/resume/$RESUME_ID/reject"
curl -s -X POST "$ADMIN_BASE/admin/audit/resume/$RESUME_ID/reject" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Accept: application/json" \
    -H "Content-Type: application/json" \
    -d '{"version":1,"reason":"信息不完整，请补充身份证明","notify":false,"block_user":false}' \
    -o "$LOG_DIR/reject.json"
python3 -c "import json; d=json.load(open('$LOG_DIR/reject.json')); print('  ', json.dumps(d, ensure_ascii=False))"

# ============================================================================
# 9. 验证最终状态
# ============================================================================
echo ""
echo "==> [9] DB 最终状态"
docker exec jobbridge-mysql mysql -uroot -proot -se "
SELECT 'job' AS type, id, owner_userid, audit_status, version
FROM jobbridge.job WHERE id=$JOB_ID
UNION
SELECT 'resume', id, owner_userid, audit_status, version
FROM jobbridge.resume WHERE id=$RESUME_ID;
" 2>&1 | grep -vE "Warning|^$"

echo ""
echo "==> [9.1] audit_log 写入"
docker exec jobbridge-mysql mysql -uroot -proot -se "
SELECT target_type, target_id, action, operator, LEFT(reason, 40) AS reason
FROM jobbridge.audit_log
WHERE target_id IN ($JOB_ID, $RESUME_ID) AND target_type IN ('job','resume')
ORDER BY id;
" 2>&1 | grep -vE "Warning|^$"

# ============================================================================
# 10. /admin/accounts/factories 是否包含 wm_mock_factory_001
# ============================================================================
echo ""
echo "==> [10] GET /admin/accounts/factories（mock 用户可见性）"
curl -s "$ADMIN_BASE/admin/accounts/factories?page=1&size=20" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Accept: application/json" \
    -o "$LOG_DIR/factories.json"
python3 <<PYEOF
import json
d = json.load(open('$LOG_DIR/factories.json'))
items = d.get('data',{}).get('items',[])
mock_factories = [i for i in items if 'wm_mock_' in str(i.get('external_userid',''))]
print(f"  factories total={d.get('data',{}).get('total','?')}，其中 wm_mock_* {len(mock_factories)} 条")
for i in mock_factories:
    print(f"    - {i.get('external_userid')} | {i.get('display_name')} | {i.get('company','-')}")
PYEOF

# ============================================================================
# 11. /admin/logs/conversations 看 mock 用户对话
# ============================================================================
echo ""
echo "==> [11] GET /admin/logs/conversations?userid=wm_mock_worker_001"
TIME_START=$(date -u -d "10 minutes ago" +"%Y-%m-%dT%H:%M:%S")
TIME_END=$(date -u +"%Y-%m-%dT%H:%M:%S")
curl -s "$ADMIN_BASE/admin/logs/conversations?userid=wm_mock_worker_001&start=$TIME_START&end=$TIME_END" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Accept: application/json" \
    -o "$LOG_DIR/logs.json"
python3 <<PYEOF
import json
d = json.load(open('$LOG_DIR/logs.json'))
items = d.get('data',{}).get('items',[])
print(f"  conversation_log code={d.get('code','?')}, 找到 {len(items)} 条")
for i in items[:5]:
    print(f"    - {i.get('direction')} | {i.get('intent','-')} | {(i.get('content') or '')[:50]}")
PYEOF

echo ""
echo "============================================================"
echo "  ✅ 管理端流程冒烟完成"
echo "============================================================"
