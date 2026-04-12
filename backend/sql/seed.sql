-- ============================================================================
-- 招聘撮合平台 种子数据
-- ============================================================================
-- 对应方案设计：方案设计_v0.1.md (v0.4)
-- 依赖：schema.sql 已经执行完毕
-- 说明：仅包含系统正常运行必需的最小种子数据
--   - 工种大类字典（10 个一级分类）
--   - 系统配置默认值
--   - 城市字典：仅示例几条，完整 340 城导入另行用脚本批量插入
--   - 敏感词字典：仅示例，完整词表由运营后台逐步维护
--   - 管理员账号：开发环境默认账号，生产部署前必须修改
-- ============================================================================

SET NAMES utf8mb4;


-- ============================================================================
-- 工种大类字典（一期 10 类，对齐 §7.3）
-- ============================================================================
INSERT INTO `dict_job_category` (`code`, `name`, `aliases`, `sort_order`, `enabled`) VALUES
('electronic_factory', '电子厂', JSON_ARRAY('电子', '电子组装', '电子装配', 'SMT'),           10, 1),
('garment_factory',    '服装厂', JSON_ARRAY('制衣', '服装', '车工', '缝纫'),                  20, 1),
('food_factory',       '食品厂', JSON_ARRAY('食品', '食品加工', '食品包装'),                  30, 1),
('logistics',          '物流仓储', JSON_ARRAY('物流', '仓库', '分拣', '快递', '打包'),        40, 1),
('catering',           '餐饮',   JSON_ARRAY('餐厅', '后厨', '服务员', '传菜', '洗碗'),         50, 1),
('cleaning',           '保洁',   JSON_ARRAY('清洁', '保洁员', '家政'),                          60, 1),
('security',           '保安',   JSON_ARRAY('安保', '门卫', '巡逻'),                            70, 1),
('skilled_worker',     '技工',   JSON_ARRAY('焊工', '电工', '钳工', '车床', '叉车'),            80, 1),
('general_worker',     '普工',   JSON_ARRAY('生产', '流水线', '操作工'),                        90, 1),
('other',              '其他',   JSON_ARRAY(),                                                999, 1);


-- ============================================================================
-- 城市字典（示例数据，正式环境用 import_cities.py 批量导入全国 340 城）
-- ============================================================================
INSERT INTO `dict_city` (`code`, `name`, `short_name`, `province`, `aliases`, `enabled`) VALUES
('320500', '苏州市',   '苏州',   '江苏省', JSON_ARRAY('姑苏', '苏州工业园区', '苏州园区'), 1),
('320100', '南京市',   '南京',   '江苏省', JSON_ARRAY('金陵'),                              1),
('320583', '昆山市',   '昆山',   '江苏省', JSON_ARRAY(),                                    1),
('310000', '上海市',   '上海',   '上海市', JSON_ARRAY('沪', '魔都'),                        1),
('440300', '深圳市',   '深圳',   '广东省', JSON_ARRAY('鹏城'),                              1),
('441900', '东莞市',   '东莞',   '广东省', JSON_ARRAY(),                                    1),
('330100', '杭州市',   '杭州',   '浙江省', JSON_ARRAY(),                                    1),
('110000', '北京市',   '北京',   '北京市', JSON_ARRAY('京'),                                1);
-- 完整城市表见 sql/seed_cities_full.sql（待生成）


-- ============================================================================
-- 系统配置默认值
-- ============================================================================
INSERT INTO `system_config` (`config_key`, `config_value`, `value_type`, `description`) VALUES
-- 数据 TTL（天）
('ttl.job.days',                  '30',  'int',  '岗位 TTL（天）'),
('ttl.resume.days',                '30', 'int',  '简历 TTL（天）'),
('ttl.conversation_log.days',      '30', 'int',  '对话日志 TTL（天）'),

-- 匹配引擎参数
('match.top_n',                    '3',  'int',  '首轮推荐条数（§10.3）'),
('match.max_candidates',           '50', 'int',  'SQL 硬过滤后送 rerank 的最大候选数'),
('match.auto_relax_on_empty',      'true', 'bool', '0 召回时是否自动放宽条件'),
('match.relax_salary_pct',         '0.1', 'string', '放宽时薪资下调比例（10%）'),

-- 敏感字段硬过滤开关（§7.5）
('filter.enable_gender',           'true', 'bool', '是否启用性别硬过滤'),
('filter.enable_age',               'true', 'bool', '是否启用年龄硬过滤'),
('filter.enable_ethnicity',         'true', 'bool', '是否启用民族硬过滤'),

-- 内容审核阈值
('audit.auto_reject_threshold',     '0.9', 'string', '自动拒绝置信度阈值'),
('audit.manual_review_threshold',   '0.5', 'string', '人工灰度置信度阈值'),

-- LLM 配置
('llm.provider',                    'qwen', 'string', '当前 LLM 供应商: qwen / doubao / local'),
('llm.intent_model',                'qwen-turbo', 'string', '意图抽取档模型名'),
('llm.reranker_model',              'qwen-plus',  'string', '重排生成档模型名'),

-- 会话状态
('session.timeout_minutes',         '30', 'int', '多轮会话超时（分钟）'),
('session.max_history_turns',       '6',  'int', '保留在上下文的最近轮数'),

-- 上传限制（§3）
('upload.max_text_chars',           '2000', 'int', '单条文字上限'),
('upload.max_images',               '5',   'int',  '单条图片数上限'),
('upload.max_image_size_kb',        '1024','int',  '单张图片大小上限（KB）'),

-- 企微群日报推送
('report.daily_push_enabled',       'true', 'bool', '是否启用每日企微群日报'),
('report.daily_push_time',          '09:00', 'string', '推送时间（HH:MM）'),
('report.daily_push_group_chatid',  '',     'string', '接收日报的企微群 chatid'),

-- 限流（防刷）
('rate_limit.window_seconds',       '10',  'int',  '限流时间窗口（秒）'),
('rate_limit.max_count',            '5',   'int',  '窗口内最大消息数');


-- ============================================================================
-- 敏感词示例（正式环境由运营后台逐步维护）
-- ============================================================================
INSERT INTO `dict_sensitive_word` (`word`, `level`, `category`, `enabled`) VALUES
('传销',         'high', '诈骗',   1),
('色情',         'high', '色情',   1),
('赌博',         'high', '诈骗',   1),
('代开发票',     'high', '诈骗',   1),
('高薪招聘少女', 'high', '诈骗',   1);


-- ============================================================================
-- 管理员账号（开发环境默认，生产部署前必须修改）
-- ============================================================================
-- 默认账号：  admin
-- 默认密码：  admin123  （bcrypt cost=10）
-- 生成命令：  python -c "import bcrypt; print(bcrypt.hashpw(b'admin123', bcrypt.gensalt(10)).decode())"
-- ⚠️ 生产部署前必须执行：
--      UPDATE admin_user SET password_hash = '<新哈希>' WHERE username = 'admin';
-- ============================================================================
INSERT INTO `admin_user` (`username`, `password_hash`, `display_name`, `password_changed`, `enabled`) VALUES
('admin', '$2b$10$eSJKksBigl05aIBiYNR/MuHvR0GCahspw0YnVo3EL8UlYanuXBNDy', '系统管理员', 0, 1);
