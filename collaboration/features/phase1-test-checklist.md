# Phase 1 测试 Checklist

> 基于：`collaboration/features/phase1-main.md`
> 面向角色：测试
> 状态：`in-progress`
> 开发交付日期：2026-04-12

## 使用方式

测试只验证 Phase 1 范围内内容：

- ORM 模型
- DTO
- 配置与基础设施契约
- SQL 初始化与导数
- Redis 基础能力

本阶段**不测**：

- 业务 service
- webhook / worker
- 企微联调
- LLM 抽取效果
- admin API

## 开发侧备注（供测试参考）

### 已交付的自动化测试

| 文件 | 测试内容 | 用例数 | 依赖 |
|---|---|---|---|
| `tests/unit/test_models.py` | 模型导入、表名、字段、DDL 类型对齐 | 40 | 无 |
| `tests/unit/test_schemas.py` | DTO 实例化、校验、序列化 | 24 | 无 |
| `tests/integration/test_db.py` | 数据库连接、建表、表数量 | 3 | MySQL |
| `tests/integration/test_redis.py` | session/dedup/rate_limit/queue/lock | 14 | Redis |

### 运行方式

参见 `backend/tests/README.md`，已提供 Linux/macOS、PowerShell、CMD 三种写法。

### 关键变更说明

1. ORM 使用 `sqlalchemy.dialects.mysql` 类型（BIGINT UNSIGNED / TINYINT / MEDIUMTEXT 等）严格对齐 DDL
2. `config.py` 新增 `redis_max_connections`（默认 50），`redis_client.py` 连接池改为读取此配置
3. `.env.example` 已同步新增 `REDIS_MAX_CONNECTIONS`

## A. 测试前准备

- [ ] 准备 MySQL 8.0+ 实例
- [ ] 准备 Redis 实例
- [ ] 准备 Python 3.11+ 环境
- [ ] 复制 `.env.example` 为本地 `.env`
- [ ] 按实际环境填写 DB / Redis 配置

## B. 初始化步骤

- [ ] 执行 `backend/sql/schema.sql`
- [ ] 执行 `backend/sql/seed.sql`
- [ ] 执行 `backend/sql/seed_cities_full.sql`
- [ ] 记录导入是否报错

预期结果：

- 11 张表均建表成功
- 种子数据导入成功
- 无字段缺失、索引冲突或 SQL 语法报错

## C. 启动验证

- [ ] 安装 `backend/requirements.txt`
- [ ] 启动 `backend/app/main.py`
- [ ] 访问 `/health`

预期结果：

- 应用可启动
- `/health` 返回中数据库连通正常

## D. ORM 验证

### D1. 基础导入

- [ ] 导入 `app.models` 不报错
- [ ] `from app.models import *` 不报错

### D2. 表覆盖验证

- [ ] `user`
- [ ] `job`
- [ ] `resume`
- [ ] `conversation_log`
- [ ] `audit_log`
- [ ] `dict_city`
- [ ] `dict_job_category`
- [ ] `dict_sensitive_word`
- [ ] `system_config`
- [ ] `admin_user`
- [ ] `wecom_inbound_event`

预期结果：

- ORM 与 `schema.sql` 当前 11 张表一一对应

### D3. 关键字段验证

- [ ] `job.version` 存在
- [ ] `resume.version` 存在
- [ ] `conversation_log.wecom_msg_id` 存在
- [ ] `conversation_log.wecom_msg_id` 唯一约束存在
- [ ] `wecom_inbound_event` 表存在
- [ ] `wecom_inbound_event.status` 枚举正确（received/processing/done/failed/dead_letter）
- [ ] `user.status` 包含 `deleted`
- [ ] `job.delist_reason` 枚举正确（filled/manual_delist/expired）

### D4. 类型与约束验证

- [ ] JSON 字段能正确读写
- [ ] 默认值与 DDL 一致
- [ ] 唯一约束与索引存在
- [ ] 可空/非空规则与 DDL 一致

### D5. DDL 类型严格对齐验证（Codex review 后新增）

- [ ] `BIGINT UNSIGNED` PK 列使用了 `mysql.BIGINT(unsigned=True)`（job/resume/conversation_log/audit_log/wecom_inbound_event）
- [ ] `INT UNSIGNED` PK 列使用了 `mysql.INTEGER(unsigned=True)`（dict_city/dict_job_category/dict_sensitive_word/admin_user）
- [ ] `job.version` / `resume.version` 使用了 `mysql.INTEGER(unsigned=True)`
- [ ] `TINYINT UNSIGNED` 列使用了 `mysql.TINYINT(unsigned=True)`（age_min/age_max/age/retry_count）
- [ ] 所有 `TINYINT(1)` 布尔字段使用了 `mysql.TINYINT`
- [ ] `resume.height` / `resume.weight` 使用了 `mysql.SMALLINT(unsigned=True)`
- [ ] `conversation_log.content` 使用了 `mysql.MEDIUMTEXT`

提示：以上检查可通过运行 `pytest tests/unit/test_models.py::TestDDLTypeAlignment -v` 自动验证。

## E. DTO 验证

- [ ] `schemas/user.py` 可正常导入与实例化
- [ ] `schemas/job.py` 可正常导入与实例化
- [ ] `schemas/resume.py` 可正常导入与实例化
- [ ] `schemas/conversation.py` 可正常导入与实例化
- [ ] `schemas/llm.py` 可正常导入与实例化
- [ ] `schemas/admin.py` 可正常导入与实例化

重点验证：

- [ ] `CandidateSnapshot` 包含 `candidate_ids`、`ranking_version`、`query_digest`、`created_at`、`expires_at`
- [ ] `SessionState` 包含 `search_criteria`、`candidate_snapshot`、`shown_items`、`history`、`updated_at`

提示：以上检查可通过运行 `pytest tests/unit/test_schemas.py -v` 自动验证。

## F. Redis 基础能力验证

- [ ] `get_session()` 正常
- [ ] `save_session()` 正常
- [ ] `delete_session()` 正常
- [ ] `check_msg_duplicate()` 正常
- [ ] `check_rate_limit()` 正常
- [ ] `enqueue_message()` / `dequeue_message()` 正常
- [ ] `user_lock()` 可获取与释放

预期结果：

- Session 有 TTL
- 重复消息可识别
- 限流函数行为正确
- 队列方法可正常入出队，保持 FIFO 顺序
- 锁可正常加锁释放，同 user 排他，不同 user 互不干扰

提示：以上检查可通过运行 `RUN_INTEGRATION=1 pytest tests/integration/test_redis.py -v`（或 PowerShell `$env:RUN_INTEGRATION='1'; pytest tests/integration/test_redis.py -v`）自动验证。

## G. 配置验证

- [ ] 开发环境未设置 `CORS_ORIGINS` 时，`cors_origin_list == ["*"]`
- [ ] 生产环境未设置 `CORS_ORIGINS` 时，`cors_origin_list == []`
- [ ] 设置多个 `CORS_ORIGINS` 时可正确解析列表
- [ ] `settings.db_url` 拼接正确
- [ ] `settings.redis_url` 拼接正确
- [ ] `settings.redis_max_connections` 默认值为 50，可通过 `REDIS_MAX_CONNECTIONS` 环境变量覆盖

## H. 自动化测试验证

- [ ] 开发提交的 Phase 1 自动化测试可运行
- [ ] 单元测试全部通过（预期 64 passed）
- [ ] 集成测试全部通过（预期 14 passed，需 MySQL + Redis）
- [ ] 自动化测试结果可证明 ORM、DTO、数据库、Redis 基础能力正常

## I. 缺陷判定标准

出现以下任一情况，Phase 1 测试判定不通过：

- [ ] ORM 与 `schema.sql` 不一致
- [ ] 11 张表缺任意一张
- [ ] `version`、`wecom_msg_id`、`wecom_inbound_event` 任一漏实现
- [ ] ORM 列类型与 DDL 不一致（UNSIGNED / TINYINT / MEDIUMTEXT）
- [ ] DTO 字段与主需求文档明显不一致
- [ ] Redis 基础方法不可用（含队列和锁）
- [ ] CORS 配置行为与主需求文档不一致
- [ ] 建表或种子数据导入失败
- [ ] 开发越界实现了 Phase 2/3/4 内容但 Phase 1 本身未闭环

## J. 测试结论输出

测试结束后请输出：

- [ ] 通过 / 不通过结论
- [ ] 不通过项清单
- [ ] 复现步骤
- [ ] 实际环境信息
- [ ] 是否可进入 Phase 2
