#!/usr/bin/env bash
# Phase 5 开发期假数据 seed 脚本
#
# 用途：在没有真机企微联调环境的情况下，为 Phase 5 admin API / Phase 6 前端
#       开发期间提供可用的列表/筛选/分页数据。
#
# 数据范围：
#   - user:                 8 条（2 factory + 2 broker + 4 worker）
#   - job:                  20 条（覆盖不同城市/工种/审核状态/下架原因）
#   - resume:               8 条（覆盖不同工人的求职简历）
#   - conversation_log:     30 条（入/出各方向，带 wecom_msg_id / NULL）
#   - wecom_inbound_event:  15 条（覆盖 received/processing/done/failed/dead_letter 全部状态）
#   - audit_log:            10 条（自动/人工通过/驳回）
#
# 所有测试数据 external_userid 前缀 `dev_`，便于 cleanup。
#
# 用法：
#   bash scripts/seed_dev_data.sh        # 全量 seed
#   bash scripts/seed_dev_data.sh clean  # 清理所有 dev_* 数据

set -euo pipefail

ACTION="${1:-seed}"

if [ "$ACTION" = "clean" ]; then
    echo "[+] 清理 dev_* 测试数据..."
    docker exec -i jobbridge-mysql mysql --default-character-set=utf8mb4 -u jobbridge -pjobbridge jobbridge <<'SQL'
DELETE FROM conversation_log   WHERE userid       LIKE 'dev_%';
DELETE FROM wecom_inbound_event WHERE from_userid  LIKE 'dev_%';
DELETE FROM audit_log          WHERE target_id    LIKE 'dev_%' OR operator LIKE 'dev_%';
DELETE FROM job                WHERE owner_userid LIKE 'dev_%';
DELETE FROM resume             WHERE owner_userid LIKE 'dev_%';
DELETE FROM user               WHERE external_userid LIKE 'dev_%';
SELECT 'user' tbl, COUNT(*) cnt FROM user  WHERE external_userid LIKE 'dev_%'
UNION ALL SELECT 'job', COUNT(*) FROM job WHERE owner_userid LIKE 'dev_%'
UNION ALL SELECT 'conv', COUNT(*) FROM conversation_log WHERE userid LIKE 'dev_%';
SQL
    echo "[✓] 清理完成"
    exit 0
fi

echo "[+] Seed Phase 5 开发假数据到 MySQL..."

docker exec -i jobbridge-mysql mysql --default-character-set=utf8mb4 -u jobbridge -pjobbridge jobbridge <<'SQL'
-- ============================================================================
-- 用户 × 8（2 厂家 + 2 中介 + 4 工人）
-- ============================================================================
INSERT IGNORE INTO `user`
  (external_userid, role, status, display_name, company, contact_person,
   phone, can_search_jobs, can_search_workers, last_active_at, registered_at)
VALUES
  ('dev_factory_001', 'factory', 'active',  '张志强', '苏州睿联电子有限公司', '张志强',
   '13800000001', 0, 1, DATE_SUB(NOW(), INTERVAL 2 HOUR),   DATE_SUB(NOW(), INTERVAL 30 DAY)),
  ('dev_factory_002', 'factory', 'active',  '李建军', '昆山锦华服装厂',       '李建军',
   '13800000002', 0, 1, DATE_SUB(NOW(), INTERVAL 1 DAY),    DATE_SUB(NOW(), INTERVAL 20 DAY)),
  ('dev_broker_001',  'broker',  'active',  '李桂芳', NULL,                    '李桂芳',
   '13800000003', 1, 1, DATE_SUB(NOW(), INTERVAL 3 HOUR),   DATE_SUB(NOW(), INTERVAL 25 DAY)),
  ('dev_broker_002',  'broker',  'blocked', '王大海', NULL,                    '王大海',
   '13800000004', 1, 1, DATE_SUB(NOW(), INTERVAL 5 DAY),    DATE_SUB(NOW(), INTERVAL 18 DAY)),
  ('dev_worker_001',  'worker',  'active',  '陈小明', NULL,                    NULL,
   '13800000005', 1, 0, DATE_SUB(NOW(), INTERVAL 1 HOUR),   DATE_SUB(NOW(), INTERVAL 10 DAY)),
  ('dev_worker_002',  'worker',  'active',  '刘小花', NULL,                    NULL,
   '13800000006', 1, 0, DATE_SUB(NOW(), INTERVAL 4 HOUR),   DATE_SUB(NOW(), INTERVAL 8 DAY)),
  ('dev_worker_003',  'worker',  'deleted', '赵老四', NULL,                    NULL,
   '13800000007', 1, 0, DATE_SUB(NOW(), INTERVAL 7 DAY),    DATE_SUB(NOW(), INTERVAL 15 DAY)),
  ('dev_worker_004',  'worker',  'active',  '孙小波', NULL,                    NULL,
   '13800000008', 1, 0, DATE_SUB(NOW(), INTERVAL 30 MINUTE), DATE_SUB(NOW(), INTERVAL 5 DAY));

-- ============================================================================
-- 岗位 × 20
-- ============================================================================
INSERT IGNORE INTO `job`
  (owner_userid, city, job_category, salary_floor_monthly, salary_ceiling_monthly,
   pay_type, headcount, gender_required, age_min, age_max, is_long_term,
   provide_meal, provide_housing, shift_pattern,
   raw_text, description,
   audit_status, audit_reason, audited_by, audited_at,
   delist_reason, expires_at, created_at)
VALUES
  -- 厂家 001 （苏州电子厂） × 5：覆盖 passed/pending/rejected + filled/manual_delist
  ('dev_factory_001', '苏州市', '电子厂', 5500, 7000, '月薪', 30, '不限', 18, 40, 1,
   1, 1, '两班倒',
   '苏州电子厂招普工30人，5500-7000月薪包吃住，18-40岁，两班倒',
   '苏州电子厂普工岗位，月薪5500-7000元，包吃住，两班倒',
   'passed', NULL, 'system', DATE_SUB(NOW(), INTERVAL 3 DAY),
   NULL, DATE_ADD(NOW(), INTERVAL 27 DAY), DATE_SUB(NOW(), INTERVAL 3 DAY)),

  ('dev_factory_001', '苏州市', '电子厂', 6000, 8000, '月薪', 20, '男', 18, 35, 1,
   1, 1, '白班',
   '苏州电子厂招SMT技术员20人，6000-8000月薪，男18-35岁，白班',
   '苏州电子厂SMT技术员岗位',
   'pending', NULL, NULL, NULL,
   NULL, DATE_ADD(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 6 HOUR)),

  ('dev_factory_001', '苏州市', '电子厂', 5000, 6500, '时薪', 15, '女', 18, 38, 1,
   1, 0, '做六休一',
   '苏州电子厂招女工15人，时薪23-28元',
   '苏州电子厂女工岗位',
   'rejected', '薪资描述不清晰，缺少每日工时', 'system', DATE_SUB(NOW(), INTERVAL 2 DAY),
   NULL, DATE_ADD(NOW(), INTERVAL 28 DAY), DATE_SUB(NOW(), INTERVAL 2 DAY)),

  ('dev_factory_001', '苏州市', '电子厂', 7000, 9000, '月薪', 10, '不限', 20, 40, 1,
   1, 1, '白班',
   '苏州电子厂招焊工技术工10人，7000-9000月薪',
   '苏州电子厂焊工岗位',
   'passed', NULL, 'system', DATE_SUB(NOW(), INTERVAL 7 DAY),
   'filled', DATE_ADD(NOW(), INTERVAL 23 DAY), DATE_SUB(NOW(), INTERVAL 7 DAY)),

  ('dev_factory_001', '苏州市', '电子厂', 4800, 5800, '月薪', 50, '不限', 18, 45, 1,
   1, 1, '三班倒',
   '苏州电子厂招流水线普工50人',
   '苏州电子厂流水线普工',
   'passed', NULL, 'system', DATE_SUB(NOW(), INTERVAL 5 DAY),
   'manual_delist', DATE_ADD(NOW(), INTERVAL 25 DAY), DATE_SUB(NOW(), INTERVAL 5 DAY)),

  -- 厂家 002（昆山服装）× 5
  ('dev_factory_002', '苏州市', '服装厂', 5500, 7500, '计件', 30, '女', 20, 45, 1,
   1, 1, '白班',
   '昆山服装厂招缝纫工30人，计件5500-7500',
   '昆山服装厂缝纫工',
   'passed', NULL, 'system', DATE_SUB(NOW(), INTERVAL 4 DAY),
   NULL, DATE_ADD(NOW(), INTERVAL 26 DAY), DATE_SUB(NOW(), INTERVAL 4 DAY)),

  ('dev_factory_002', '苏州市', '服装厂', 4500, 6000, '月薪', 20, '女', 18, 40, 1,
   1, 0, '白班',
   '昆山服装厂招熨烫工20人',
   '昆山服装厂熨烫工',
   'passed', NULL, 'admin001', DATE_SUB(NOW(), INTERVAL 1 DAY),
   NULL, DATE_ADD(NOW(), INTERVAL 29 DAY), DATE_SUB(NOW(), INTERVAL 1 DAY)),

  ('dev_factory_002', '苏州市', '服装厂', 5000, 6500, '月薪', 10, '不限', 18, 45, 1,
   1, 1, '做六休一',
   '昆山服装厂招质检员10人',
   '昆山服装厂质检员',
   'pending', NULL, NULL, NULL,
   NULL, DATE_ADD(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 2 HOUR)),

  ('dev_factory_002', '苏州市', '服装厂', 6000, 8500, '计件', 15, '女', 22, 50, 1,
   1, 1, '白班',
   '昆山服装厂招高级车工15人，需3年经验',
   '昆山服装厂高级车工',
   'passed', NULL, 'admin001', DATE_SUB(NOW(), INTERVAL 10 DAY),
   NULL, DATE_ADD(NOW(), INTERVAL 20 DAY), DATE_SUB(NOW(), INTERVAL 10 DAY)),

  ('dev_factory_002', '苏州市', '服装厂', 4200, 5500, '月薪', 25, '不限', 18, 40, 1,
   1, 1, '两班倒',
   '昆山服装厂招包装工25人',
   '昆山服装厂包装工',
   'rejected', '工作地址表述含糊', 'admin001', DATE_SUB(NOW(), INTERVAL 15 DAY),
   NULL, DATE_ADD(NOW(), INTERVAL 15 DAY), DATE_SUB(NOW(), INTERVAL 15 DAY)),

  -- 中介 001（无锡/苏州多工种）× 10
  ('dev_broker_001', '无锡市', '电子厂', 5800, 7200, '月薪', 40, '不限', 18, 42, 1,
   1, 1, '两班倒',
   '无锡电子厂招普工40人，5800-7200包吃住',
   '无锡电子厂普工',
   'passed', NULL, 'system', DATE_SUB(NOW(), INTERVAL 3 DAY),
   NULL, DATE_ADD(NOW(), INTERVAL 27 DAY), DATE_SUB(NOW(), INTERVAL 3 DAY)),

  ('dev_broker_001', '无锡市', '物流仓储', 5200, 6500, '月薪', 30, '男', 20, 45, 1,
   1, 0, '白班',
   '无锡物流园招分拣员30人',
   '无锡物流园分拣员',
   'passed', NULL, 'system', DATE_SUB(NOW(), INTERVAL 5 DAY),
   NULL, DATE_ADD(NOW(), INTERVAL 25 DAY), DATE_SUB(NOW(), INTERVAL 5 DAY)),

  ('dev_broker_001', '无锡市', '物流仓储', 6000, 8000, '时薪', 15, '男', 22, 50, 1,
   0, 0, '白班',
   '无锡物流公司招叉车司机15人，需叉车证',
   '无锡物流叉车司机',
   'passed', NULL, 'system', DATE_SUB(NOW(), INTERVAL 6 DAY),
   NULL, DATE_ADD(NOW(), INTERVAL 24 DAY), DATE_SUB(NOW(), INTERVAL 6 DAY)),

  ('dev_broker_001', '无锡市', '普工',   4500, 5800, '月薪', 60, '不限', 18, 50, 1,
   1, 1, '三班倒',
   '无锡制造业招普工60人',
   '无锡制造业普工',
   'passed', NULL, 'system', DATE_SUB(NOW(), INTERVAL 2 DAY),
   NULL, DATE_ADD(NOW(), INTERVAL 28 DAY), DATE_SUB(NOW(), INTERVAL 2 DAY)),

  ('dev_broker_001', '无锡市', '电子厂', 5000, 6000, '月薪', 20, '女', 18, 40, 1,
   1, 1, '白班',
   '无锡电子厂招女工20人',
   '无锡电子厂女工',
   'pending', NULL, NULL, NULL,
   NULL, DATE_ADD(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 1 HOUR)),

  ('dev_broker_001', '苏州市', '物流仓储', 5500, 7000, '月薪', 25, '不限', 20, 45, 1,
   1, 1, '两班倒',
   '苏州物流园招装卸工25人',
   '苏州物流装卸工',
   'passed', NULL, 'system', DATE_SUB(NOW(), INTERVAL 8 DAY),
   NULL, DATE_ADD(NOW(), INTERVAL 22 DAY), DATE_SUB(NOW(), INTERVAL 8 DAY)),

  ('dev_broker_001', '苏州市', '食品厂', 4800, 6000, '月薪', 30, '不限', 18, 45, 1,
   1, 1, '白班',
   '苏州食品厂招包装工30人',
   '苏州食品厂包装工',
   'passed', NULL, 'system', DATE_SUB(NOW(), INTERVAL 4 DAY),
   'filled', DATE_ADD(NOW(), INTERVAL 26 DAY), DATE_SUB(NOW(), INTERVAL 4 DAY)),

  ('dev_broker_001', '苏州市', '技工',   7500, 10000, '月薪', 8, '男', 25, 50, 1,
   1, 1, '白班',
   '苏州工厂招焊工8人，需3年以上经验',
   '苏州工厂焊工',
   'passed', NULL, 'system', DATE_SUB(NOW(), INTERVAL 12 DAY),
   NULL, DATE_ADD(NOW(), INTERVAL 18 DAY), DATE_SUB(NOW(), INTERVAL 12 DAY)),

  ('dev_broker_001', '无锡市', '服装厂', 4800, 6200, '计件', 20, '女', 20, 45, 1,
   1, 0, '白班',
   '无锡服装厂招女工20人',
   '无锡服装厂女工',
   'rejected', '与实际工作内容不符', 'admin001', DATE_SUB(NOW(), INTERVAL 20 DAY),
   NULL, DATE_ADD(NOW(), INTERVAL 10 DAY), DATE_SUB(NOW(), INTERVAL 20 DAY)),

  ('dev_broker_001', '苏州市', '餐饮',   4500, 5800, '月薪', 12, '不限', 18, 45, 1,
   1, 1, '两班倒',
   '苏州连锁餐厅招服务员12人',
   '苏州餐厅服务员',
   'passed', NULL, 'system', DATE_SUB(NOW(), INTERVAL 1 DAY),
   NULL, DATE_ADD(NOW(), INTERVAL 29 DAY), DATE_SUB(NOW(), INTERVAL 1 DAY));

-- ============================================================================
-- 简历 × 8
-- ============================================================================
INSERT IGNORE INTO `resume`
  (owner_userid, expected_cities, expected_job_categories,
   salary_expect_floor_monthly, gender, age,
   accept_long_term, accept_short_term,
   raw_text, description,
   audit_status, audited_by, audited_at, expires_at, created_at)
VALUES
  ('dev_worker_001', JSON_ARRAY('苏州市','无锡市'), JSON_ARRAY('电子厂','普工'),
   5500, '男', 25, 1, 0,
   '苏州找电子厂，5500以上，包吃住', '苏州电子厂求职',
   'passed', 'system', DATE_SUB(NOW(), INTERVAL 1 DAY),
   DATE_ADD(NOW(), INTERVAL 29 DAY), DATE_SUB(NOW(), INTERVAL 1 DAY)),

  ('dev_worker_002', JSON_ARRAY('苏州市'), JSON_ARRAY('服装厂'),
   5000, '女', 32, 1, 1,
   '苏州服装厂找工作，有3年经验', '苏州服装厂求职',
   'passed', 'system', DATE_SUB(NOW(), INTERVAL 2 DAY),
   DATE_ADD(NOW(), INTERVAL 28 DAY), DATE_SUB(NOW(), INTERVAL 2 DAY)),

  ('dev_worker_003', JSON_ARRAY('无锡市'), JSON_ARRAY('普工'),
   4500, '男', 40, 1, 0,
   '无锡普工，能吃苦', '无锡普工求职',
   'passed', 'system', DATE_SUB(NOW(), INTERVAL 15 DAY),
   DATE_ADD(NOW(), INTERVAL 15 DAY), DATE_SUB(NOW(), INTERVAL 15 DAY)),

  ('dev_worker_004', JSON_ARRAY('苏州市','昆山市'), JSON_ARRAY('电子厂','物流仓储'),
   6000, '男', 28, 1, 0,
   '找苏州电子厂或物流，6000以上', '苏州电子厂/物流求职',
   'pending', NULL, NULL,
   DATE_ADD(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 3 HOUR)),

  ('dev_worker_001', JSON_ARRAY('苏州市'), JSON_ARRAY('技工'),
   7000, '男', 25, 1, 0,
   '苏州找焊工工作，有焊工证', '苏州焊工求职',
   'rejected', 'system', DATE_SUB(NOW(), INTERVAL 5 DAY),
   DATE_ADD(NOW(), INTERVAL 25 DAY), DATE_SUB(NOW(), INTERVAL 5 DAY)),

  ('dev_worker_002', JSON_ARRAY('苏州市','无锡市'), JSON_ARRAY('食品厂','餐饮'),
   4500, '女', 32, 1, 1,
   '食品厂或餐饮都可以，长期短期都行', '苏州食品/餐饮求职',
   'passed', 'admin001', DATE_SUB(NOW(), INTERVAL 7 DAY),
   DATE_ADD(NOW(), INTERVAL 23 DAY), DATE_SUB(NOW(), INTERVAL 7 DAY)),

  ('dev_worker_004', JSON_ARRAY('无锡市'), JSON_ARRAY('普工','电子厂'),
   5200, '男', 28, 1, 0,
   '无锡普工或电子厂都行，能倒班', '无锡求职',
   'passed', 'system', DATE_SUB(NOW(), INTERVAL 4 DAY),
   DATE_ADD(NOW(), INTERVAL 26 DAY), DATE_SUB(NOW(), INTERVAL 4 DAY)),

  ('dev_worker_001', JSON_ARRAY('苏州市'), JSON_ARRAY('保安','普工'),
   4800, '男', 25, 1, 0,
   '苏州保安或普工，希望白班为主', '苏州保安/普工求职',
   'passed', 'system', DATE_SUB(NOW(), INTERVAL 10 DAY),
   DATE_ADD(NOW(), INTERVAL 20 DAY), DATE_SUB(NOW(), INTERVAL 10 DAY));

-- ============================================================================
-- 对话日志 × 30（入站 15 + 出站 15，含不同意图）
-- ============================================================================
INSERT IGNORE INTO `conversation_log`
  (userid, direction, msg_type, content, wecom_msg_id, intent, expires_at, created_at)
VALUES
  ('dev_worker_001', 'in',  'text', '你好',                      CONCAT('dev_msg_', UUID_SHORT()), 'chitchat',
   DATE_ADD(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 10 DAY)),
  ('dev_worker_001', 'out', 'text', '您好，欢迎使用 JobBridge...', NULL,                          'chitchat',
   DATE_ADD(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 10 DAY)),
  ('dev_worker_001', 'in',  'text', '苏州找电子厂，5500以上',       CONCAT('dev_msg_', UUID_SHORT()), 'search_job',
   DATE_ADD(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 10 DAY)),
  ('dev_worker_001', 'out', 'text', '为您找到 3 个岗位...',        NULL,                          'search_job',
   DATE_ADD(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 10 DAY)),
  ('dev_worker_001', 'in',  'text', '更多',                      CONCAT('dev_msg_', UUID_SHORT()), 'show_more',
   DATE_ADD(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 10 DAY)),
  ('dev_worker_001', 'out', 'text', '为您继续推荐 3 个...',        NULL,                          'show_more',
   DATE_ADD(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 10 DAY)),

  ('dev_worker_002', 'in',  'text', '苏州服装厂',                CONCAT('dev_msg_', UUID_SHORT()), 'search_job',
   DATE_ADD(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 8 DAY)),
  ('dev_worker_002', 'out', 'text', '为您找到 2 个岗位...',        NULL,                          'search_job',
   DATE_ADD(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 8 DAY)),
  ('dev_worker_002', 'in',  'text', '工资高一点的',              CONCAT('dev_msg_', UUID_SHORT()), 'follow_up',
   DATE_ADD(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 8 DAY)),
  ('dev_worker_002', 'out', 'text', '已调整条件，为您重新推荐...', NULL,                          'follow_up',
   DATE_ADD(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 8 DAY)),

  ('dev_factory_001', 'in',  'text', '苏州电子厂招普工30人，5500-7000月薪包吃住', CONCAT('dev_msg_', UUID_SHORT()), 'upload_job',
   DATE_ADD(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 3 DAY)),
  ('dev_factory_001', 'out', 'text', '您的岗位信息已入库...',    NULL,                          'upload_job',
   DATE_ADD(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 3 DAY)),
  ('dev_factory_001', 'in',  'text', '/我的状态',                CONCAT('dev_msg_', UUID_SHORT()), 'command',
   DATE_ADD(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 2 HOUR)),
  ('dev_factory_001', 'out', 'text', '账号状态：正常...',        NULL,                          'command',
   DATE_ADD(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 2 HOUR)),

  ('dev_broker_001', 'in',  'text', '/找岗位',                   CONCAT('dev_msg_', UUID_SHORT()), 'command',
   DATE_ADD(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 3 HOUR)),
  ('dev_broker_001', 'out', 'text', '已切换到找岗位模式...',      NULL,                          'command',
   DATE_ADD(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 3 HOUR)),
  ('dev_broker_001', 'in',  'text', '无锡电子厂',                CONCAT('dev_msg_', UUID_SHORT()), 'search_job',
   DATE_ADD(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 3 HOUR)),
  ('dev_broker_001', 'out', 'text', '为您找到 5 个岗位...',      NULL,                          'search_job',
   DATE_ADD(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 3 HOUR)),

  ('dev_worker_004', 'in',  'text', '你好',                     CONCAT('dev_msg_', UUID_SHORT()), 'chitchat',
   DATE_ADD(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 5 DAY)),
  ('dev_worker_004', 'out', 'text', '您好，欢迎...',              NULL,                          'chitchat',
   DATE_ADD(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 5 DAY)),
  ('dev_worker_004', 'in',  'text', '苏州电子厂或物流，6000以上', CONCAT('dev_msg_', UUID_SHORT()), 'upload_resume',
   DATE_ADD(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 5 DAY)),
  ('dev_worker_004', 'out', 'text', '简历已提交...',             NULL,                          'upload_resume',
   DATE_ADD(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 5 DAY)),

  ('dev_worker_003', 'in',  'text', '/删除我的信息',              CONCAT('dev_msg_', UUID_SHORT()), 'command',
   DATE_ADD(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 7 DAY)),
  ('dev_worker_003', 'out', 'text', '已收到您的删除请求...',      NULL,                          'command',
   DATE_ADD(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 7 DAY)),

  ('dev_broker_002', 'in',  'text', '这个账号怎么了',             CONCAT('dev_msg_', UUID_SHORT()), 'chitchat',
   DATE_ADD(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 1 DAY)),
  ('dev_broker_002', 'out', 'text', '您的账号已被限制使用...',    NULL,                          NULL,
   DATE_ADD(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 1 DAY)),

  ('dev_factory_002', 'in',  'text', '/招满了',                  CONCAT('dev_msg_', UUID_SHORT()), 'command',
   DATE_ADD(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 1 DAY)),
  ('dev_factory_002', 'out', 'text', '已标记岗位【#x】为招满...', NULL,                          'command',
   DATE_ADD(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 1 DAY)),
  ('dev_factory_002', 'in',  'text', '/续期 30',                 CONCAT('dev_msg_', UUID_SHORT()), 'command',
   DATE_ADD(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 1 HOUR)),
  ('dev_factory_002', 'out', 'text', '岗位续期 30 天成功...',    NULL,                          'command',
   DATE_ADD(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 1 HOUR));

-- ============================================================================
-- wecom_inbound_event × 15（覆盖全部 status）
-- ============================================================================
INSERT IGNORE INTO `wecom_inbound_event`
  (msg_id, from_userid, msg_type, media_id, content_brief, status,
   retry_count, worker_started_at, worker_finished_at, error_message, created_at)
VALUES
  (CONCAT('dev_ev_', UUID_SHORT()), 'dev_worker_001', 'text',  NULL, '你好',                    'done',         0,
   DATE_SUB(NOW(), INTERVAL 10 DAY), DATE_SUB(NOW(), INTERVAL 10 DAY), NULL, DATE_SUB(NOW(), INTERVAL 10 DAY)),
  (CONCAT('dev_ev_', UUID_SHORT()), 'dev_worker_001', 'text',  NULL, '苏州找电子厂，5500以上',  'done',         0,
   DATE_SUB(NOW(), INTERVAL 10 DAY), DATE_SUB(NOW(), INTERVAL 10 DAY), NULL, DATE_SUB(NOW(), INTERVAL 10 DAY)),
  (CONCAT('dev_ev_', UUID_SHORT()), 'dev_worker_002', 'text',  NULL, '苏州服装厂',              'done',         0,
   DATE_SUB(NOW(), INTERVAL 8 DAY),  DATE_SUB(NOW(), INTERVAL 8 DAY),  NULL, DATE_SUB(NOW(), INTERVAL 8 DAY)),
  (CONCAT('dev_ev_', UUID_SHORT()), 'dev_factory_001','text',  NULL, '苏州电子厂招普工30人',    'done',         0,
   DATE_SUB(NOW(), INTERVAL 3 DAY),  DATE_SUB(NOW(), INTERVAL 3 DAY),  NULL, DATE_SUB(NOW(), INTERVAL 3 DAY)),
  (CONCAT('dev_ev_', UUID_SHORT()), 'dev_broker_001', 'text',  NULL, '/找岗位',                 'done',         0,
   DATE_SUB(NOW(), INTERVAL 3 HOUR),  DATE_SUB(NOW(), INTERVAL 3 HOUR),  NULL, DATE_SUB(NOW(), INTERVAL 3 HOUR)),
  (CONCAT('dev_ev_', UUID_SHORT()), 'dev_worker_001', 'image', 'MEDIA_abc1', '[image] media_id saved', 'done', 0,
   DATE_SUB(NOW(), INTERVAL 2 DAY), DATE_SUB(NOW(), INTERVAL 2 DAY), NULL, DATE_SUB(NOW(), INTERVAL 2 DAY)),
  (CONCAT('dev_ev_', UUID_SHORT()), 'dev_worker_002', 'voice', 'MEDIA_voc1', '[voice] media_id saved', 'done', 0,
   DATE_SUB(NOW(), INTERVAL 1 DAY), DATE_SUB(NOW(), INTERVAL 1 DAY), NULL, DATE_SUB(NOW(), INTERVAL 1 DAY)),
  (CONCAT('dev_ev_', UUID_SHORT()), 'dev_factory_002','text',  NULL, '/招满了',                 'done',         0,
   DATE_SUB(NOW(), INTERVAL 1 DAY), DATE_SUB(NOW(), INTERVAL 1 DAY), NULL, DATE_SUB(NOW(), INTERVAL 1 DAY)),
  (CONCAT('dev_ev_', UUID_SHORT()), 'dev_factory_002','text',  NULL, '/续期 30',                'done',         0,
   DATE_SUB(NOW(), INTERVAL 1 HOUR), DATE_SUB(NOW(), INTERVAL 1 HOUR), NULL, DATE_SUB(NOW(), INTERVAL 1 HOUR)),

  -- 不同状态
  (CONCAT('dev_ev_', UUID_SHORT()), 'dev_worker_004', 'text',  NULL, '苏州电子厂或物流',        'received',     0,
   NULL, NULL, NULL, DATE_SUB(NOW(), INTERVAL 5 MINUTE)),
  (CONCAT('dev_ev_', UUID_SHORT()), 'dev_worker_001', 'text',  NULL, '苏州保安',                'processing',   0,
   DATE_SUB(NOW(), INTERVAL 30 SECOND), NULL, NULL, DATE_SUB(NOW(), INTERVAL 1 MINUTE)),
  (CONCAT('dev_ev_', UUID_SHORT()), 'dev_worker_002', 'text',  NULL, 'LLM 超时',                'failed',       1,
   DATE_SUB(NOW(), INTERVAL 2 MINUTE), DATE_SUB(NOW(), INTERVAL 1 MINUTE), 'TimeoutError: LLM request timeout',
   DATE_SUB(NOW(), INTERVAL 2 MINUTE)),
  (CONCAT('dev_ev_', UUID_SHORT()), 'dev_worker_001', 'text',  NULL, '处理异常 3 次',           'dead_letter',  3,
   DATE_SUB(NOW(), INTERVAL 10 MINUTE), DATE_SUB(NOW(), INTERVAL 9 MINUTE),
   'RuntimeError: unknown error after 3 retries', DATE_SUB(NOW(), INTERVAL 10 MINUTE)),
  (CONCAT('dev_ev_', UUID_SHORT()), 'dev_worker_003', 'text',  NULL, '/删除我的信息',           'done',         0,
   DATE_SUB(NOW(), INTERVAL 7 DAY), DATE_SUB(NOW(), INTERVAL 7 DAY), NULL, DATE_SUB(NOW(), INTERVAL 7 DAY)),
  (CONCAT('dev_ev_', UUID_SHORT()), 'dev_broker_002', 'text',  NULL, '这个账号怎么了',           'done',         0,
   DATE_SUB(NOW(), INTERVAL 1 DAY), DATE_SUB(NOW(), INTERVAL 1 DAY), NULL, DATE_SUB(NOW(), INTERVAL 1 DAY));

-- ============================================================================
-- audit_log × 10
-- ============================================================================
INSERT IGNORE INTO `audit_log`
  (target_type, target_id, action, reason, operator, snapshot, created_at)
VALUES
  ('job',    'dev_job_1', 'auto_pass',     '敏感词检查通过',   'system',   NULL, DATE_SUB(NOW(), INTERVAL 3 DAY)),
  ('job',    'dev_job_3', 'auto_reject',   '薪资描述不清晰',   'system',   NULL, DATE_SUB(NOW(), INTERVAL 2 DAY)),
  ('job',    'dev_job_7', 'manual_pass',   NULL,              'admin001', NULL, DATE_SUB(NOW(), INTERVAL 1 DAY)),
  ('job',    'dev_job_10','manual_reject', '工作地址表述含糊', 'admin001', NULL, DATE_SUB(NOW(), INTERVAL 15 DAY)),
  ('resume', 'dev_res_5', 'auto_reject',   '疑似重复提交',     'system',   NULL, DATE_SUB(NOW(), INTERVAL 5 DAY)),
  ('resume', 'dev_res_6', 'manual_pass',   NULL,              'admin001', NULL, DATE_SUB(NOW(), INTERVAL 7 DAY)),
  ('user',   'dev_broker_002', 'auto_reject','连续触发黑名单',  'system',   NULL, DATE_SUB(NOW(), INTERVAL 2 DAY)),
  ('user',   'dev_worker_003', 'auto_pass','用户主动执行 /删除我的信息', 'system', NULL, DATE_SUB(NOW(), INTERVAL 7 DAY)),
  ('job',    'dev_job_4', 'auto_pass',     'user_filled_job', 'dev_factory_001',
   JSON_OBJECT('delist_reason','filled'),  DATE_SUB(NOW(), INTERVAL 4 DAY)),
  ('job',    'dev_job_5', 'auto_pass',     'user_delist_job', 'dev_factory_001',
   JSON_OBJECT('delist_reason','manual_delist'), DATE_SUB(NOW(), INTERVAL 5 DAY));

-- ============================================================================
-- 结果自查
-- ============================================================================
SELECT 'user'                  AS tbl, COUNT(*) AS cnt FROM user  WHERE external_userid LIKE 'dev_%'
UNION ALL SELECT 'job',                COUNT(*) FROM job WHERE owner_userid LIKE 'dev_%'
UNION ALL SELECT 'resume',             COUNT(*) FROM resume WHERE owner_userid LIKE 'dev_%'
UNION ALL SELECT 'conversation_log',   COUNT(*) FROM conversation_log WHERE userid LIKE 'dev_%'
UNION ALL SELECT 'wecom_inbound_event', COUNT(*) FROM wecom_inbound_event WHERE from_userid LIKE 'dev_%'
UNION ALL SELECT 'audit_log',          COUNT(*) FROM audit_log WHERE operator LIKE 'dev_%' OR target_id LIKE 'dev_%';
SQL

echo ""
echo "[✓] Seed 完成。可用于 Phase 5 admin API / Phase 6 前端开发联调。"
echo ""
echo "清理：bash scripts/seed_dev_data.sh clean"
