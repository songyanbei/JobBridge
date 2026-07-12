-- =============================================================================
-- Demo 演示补充种子（针对 9 步演示脚本，特别是 "工资 9500 以上 → 薪资区间直接命中"）
--
-- 用法（在 reset_and_seed_data.py 之后追加执行）：
--   docker exec -i jobbridge-mysql \
--     mysql -u jobbridge -pjobbridge jobbridge < scripts/demo_seed_supplement.sql
--
-- 或本机：
--   mysql -h 127.0.0.1 -u jobbridge -pjobbridge jobbridge < scripts/demo_seed_supplement.sql
--
-- 设计目标：
--   - 苏州电子厂当前只有 3 条岗位（薪资下限 5400/5600/6300），密度太低
--   - 工人查询 "苏州电子厂 5500" 命中数过少，rerank Top3 不饱满
--   - "工资再高点 7000+" 当前 0 命中，但用户期望是"成功换条件"
--   - "工资 9500+" 应按岗位薪资区间上限直接命中 8600~11000 等岗位
--
-- 卡位策略（7 条苏州电子厂岗位）：
--   薪资下限 5500 / 6500 / 7000 / 7500 → 第 5、6 步候选池
--   薪资区间 8600~11000 / 8800~11500 / 9000~12000 → 第 8 步 9500+ 直接命中
--
-- 重复执行：所有 INSERT 用 demo_supp_* 标记到 extra.demo_tag，可通过下方 cleanup 段重置
-- =============================================================================

SET NAMES utf8mb4;

-- ---------- 清理上次补充（重复执行安全）----------
DELETE FROM job WHERE JSON_EXTRACT(extra, '$.demo_tag') = 'demo_supp_v1';

-- ---------- 7 条苏州电子厂剧本卡位 ----------
INSERT INTO job (
  owner_userid, city, job_category, salary_floor_monthly, salary_ceiling_monthly,
  pay_type, headcount, gender_required, age_min, age_max, is_long_term,
  district, address, provide_meal, provide_housing, shift_pattern,
  raw_text, description,
  audit_status, audited_by, audited_at,
  expires_at, extra
) VALUES
-- 第 5 步候选（"5500 包吃住"）
('seed_factory_014', '苏州市', '电子厂', 5500, 7200, '月薪', 30, '不限', 18, 45, 1,
 '工业园区', '工业园区星湖街 168 号',1, 1, '两班倒',
 '苏州工业园电子厂招普工 30 人，5500-7200，包吃住，两班倒',
 '苏州工业园电子厂普工 5500-7200 包吃住 两班倒',
 'passed', 'system', NOW(),
 DATE_ADD(NOW(), INTERVAL 60 DAY), JSON_OBJECT('demo_tag','demo_supp_v1','demo_step','5')),

('seed_factory_018', '苏州市', '电子厂', 6500, 8500, '月薪', 20, '不限', 18, 45, 1,
 '高新区', '高新区科技路 88 号',1, 1, '白班+小夜班',
 '苏州高新区电子厂招 20 人，6500-8500，包吃住，白班+小夜班',
 '苏州高新区电子厂 6500-8500 包吃住',
 'passed', 'system', NOW(),
 DATE_ADD(NOW(), INTERVAL 60 DAY), JSON_OBJECT('demo_tag','demo_supp_v1','demo_step','5')),

('seed_factory_014', '苏州市', '电子厂', 7000, 9000, '月薪', 15, '不限', 20, 42, 1,
 '昆山市', '昆山市花桥经济开发区祥和路 32 号',1, 1, '两班倒',
 '苏州昆山电子厂招熟练工 15 人，7000-9000，包吃住',
 '苏州昆山电子厂熟练工 7000-9000',
 'passed', 'system', NOW(),
 DATE_ADD(NOW(), INTERVAL 60 DAY), JSON_OBJECT('demo_tag','demo_supp_v1','demo_step','6')),

('seed_factory_018', '苏州市', '电子厂', 7500, 9800, '月薪', 10, '不限', 22, 45, 1,
 '吴中区', '吴中区东吴南路 666 号',1, 1, '白班',
 '苏州吴中电子厂招技术员 10 人，7500-9800，包吃住，白班',
 '苏州吴中电子厂技术员 7500-9800 白班',
 'passed', 'system', NOW(),
 DATE_ADD(NOW(), INTERVAL 60 DAY), JSON_OBJECT('demo_tag','demo_supp_v1','demo_step','6')),

-- 第 8 步候选（按薪资区间上限覆盖 9500 直接命中）
('seed_factory_014', '苏州市', '电子厂', 8600, 11000, '月薪', 8, '不限', 22, 45, 1,
 '工业园区', '工业园区现代大道 2008 号',1, 1, '白班',
 '苏州工业园电子厂招高级技工 8 人，8600-11000，包吃住，白班',
 '苏州工业园电子厂高级技工 8600-11000',
 'passed', 'system', NOW(),
 DATE_ADD(NOW(), INTERVAL 60 DAY), JSON_OBJECT('demo_tag','demo_supp_v1','demo_step','8')),

('seed_factory_018', '苏州市', '电子厂', 8800, 11500, '月薪', 5, '不限', 24, 45, 1,
 '相城区', '相城区华阳路 99 号',1, 1, '白班',
 '苏州相城电子厂招资深工程师 5 人，8800-11500，包吃住，白班',
 '苏州相城电子厂资深工程师 8800-11500',
 'passed', 'system', NOW(),
 DATE_ADD(NOW(), INTERVAL 60 DAY), JSON_OBJECT('demo_tag','demo_supp_v1','demo_step','8')),

('seed_factory_014', '苏州市', '电子厂', 9000, 12000, '月薪', 3, '不限', 25, 45, 1,
 '工业园区', '工业园区苏虹中路 1188 号',1, 1, '白班',
 '苏州工业园电子厂招组长 3 人，9000-12000，包吃住，白班',
 '苏州工业园电子厂组长 9000-12000',
 'passed', 'system', NOW(),
 DATE_ADD(NOW(), INTERVAL 60 DAY), JSON_OBJECT('demo_tag','demo_supp_v1','demo_step','8'));

-- ---------- 验证 ----------
-- 苏州电子厂全量分布
SELECT '苏州电子厂 全量分布' AS section;
SELECT id, salary_floor_monthly, salary_ceiling_monthly, headcount, audit_status
FROM job WHERE city='苏州市' AND job_category='电子厂' AND deleted_at IS NULL
ORDER BY salary_floor_monthly;

-- 第 5 步预期：岗位区间覆盖 5500 候选（应 ≥ 7 条）
SELECT '第 5 步 5500+ 区间命中数' AS section, COUNT(*) AS cnt
FROM job WHERE city='苏州市' AND job_category='电子厂'
  AND (salary_ceiling_monthly >= 5500 OR (salary_ceiling_monthly IS NULL AND salary_floor_monthly >= 5500))
  AND audit_status='passed' AND deleted_at IS NULL;

-- 第 6 步预期：岗位区间覆盖 7000 候选（应 ≥ 7 条）
SELECT '第 6 步 7000+ 区间命中数' AS section, COUNT(*) AS cnt
FROM job WHERE city='苏州市' AND job_category='电子厂'
  AND (salary_ceiling_monthly >= 7000 OR (salary_ceiling_monthly IS NULL AND salary_floor_monthly >= 7000))
  AND audit_status='passed' AND deleted_at IS NULL;

-- 第 8 步预期：岗位区间覆盖 9500，应直接命中 ≥ 3 条
SELECT '第 8 步 9500+ 区间命中数（应 ≥ 3）' AS section, COUNT(*) AS cnt
FROM job WHERE city='苏州市' AND job_category='电子厂'
  AND (salary_ceiling_monthly >= 9500 OR (salary_ceiling_monthly IS NULL AND salary_floor_monthly >= 9500))
  AND audit_status='passed' AND deleted_at IS NULL;
