-- ===========================================================================
-- Mock 企业微信测试台 · 用户 seed
-- ===========================================================================
--
-- 向主库 user 表幂等注入 wm_mock_* 前缀的测试身份。
-- 反复执行安全（ON DUPLICATE KEY UPDATE）。
--
-- 注意事项：
--   - 所有 external_userid 必须以 'wm_mock_' 开头，业务统计口径可据此排除
--   - role 枚举与主后端 backend/app/models.py User.role 严格对齐
--   - registered_at / extra 等默认值由表默认值填充，无需显式指定
--
-- 清理：
--   DELETE FROM user WHERE external_userid LIKE 'wm_mock_%';
-- ===========================================================================

INSERT INTO user (
  external_userid,
  role,
  display_name,
  company,
  contact_person,
  phone,
  can_search_jobs,
  can_search_workers,
  status
)
VALUES
  -- 求职者 × 2
  ('wm_mock_worker_001',  'worker',  '张工',       NULL,               '张工',   '13800000001', 1, 0, 'active'),
  ('wm_mock_worker_002',  'worker',  '李师傅',     NULL,               '李师傅', '13800000002', 1, 0, 'active'),

  -- 厂家（招聘者）× 1
  ('wm_mock_factory_001', 'factory', '华东电子厂', '华东电子有限公司', '王经理', '13900000001', 0, 1, 'active'),

  -- 中介（招聘者）× 1
  ('wm_mock_broker_001',  'broker',  '速聘中介',   '速聘人力资源',     '赵中介', '13700000001', 0, 1, 'active')
ON DUPLICATE KEY UPDATE
  role           = VALUES(role),
  display_name   = VALUES(display_name),
  company        = VALUES(company),
  contact_person = VALUES(contact_person),
  phone          = VALUES(phone),
  can_search_jobs    = VALUES(can_search_jobs),
  can_search_workers = VALUES(can_search_workers),
  status         = VALUES(status);
