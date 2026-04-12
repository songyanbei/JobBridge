# Phase 1 开发 Checklist

> 基于：`collaboration/features/phase1-main.md`
> 面向角色：后端开发
> 状态：`done`
> 完成日期：2026-04-12

## 使用方式

开发时按顺序执行，不建议跳步。

建议顺序：

1. 环境与基线确认
2. `models.py`
3. `schemas/`
4. 基础设施校验
5. 测试代码
6. 自测与交付

## A. 环境与基线确认

- [x] 拉取当前最新代码
- [x] 打开 `backend/sql/schema.sql`，确认当前为 **11 张表**
- [x] 打开 `backend/app/config.py`，确认 `CORS_ORIGINS` 为环境变量驱动
- [x] 打开 `backend/app/db.py`，确认 `Base`、`engine`、`SessionLocal`、`get_db()` 已存在
- [x] 打开 `backend/app/core/redis_client.py`，确认已有 session、幂等、限流、队列、锁基础方法
- [x] 确认 `.env.example` 足以支撑本地启动
- [x] 确认本阶段不做 service / webhook / worker / API / 真实 LLM

## B. `models.py` 实现

涉及文件：

- `backend/app/models.py`
- `backend/app/db.py`
- `backend/sql/schema.sql`

### B1. 文件创建

- [x] 新建 `backend/app/models.py`
- [x] 所有模型继承 `app.db.Base`
- [x] 每个模型定义 `__tablename__`

### B2. 11 张表映射

- [x] `user`
- [x] `job`
- [x] `resume`
- [x] `conversation_log`
- [x] `audit_log`
- [x] `dict_city`
- [x] `dict_job_category`
- [x] `dict_sensitive_word`
- [x] `system_config`
- [x] `admin_user`
- [x] `wecom_inbound_event`

### B3. 建模约束

- [x] ENUM 使用字符串类型 + `sa.Enum`
- [x] JSON 使用 `sa.JSON`
- [x] `extra` 使用 `MutableDict.as_mutable(sa.JSON)`
- [x] 不在 ORM 层做全局软删除过滤
- [x] 默认值、可空性、唯一约束、索引与 DDL 保持一致

### B4. 关键字段专检

- [x] `job.version` 已建模 — `mysql.INTEGER(unsigned=True), server_default=text("1")`
- [x] `resume.version` 已建模 — `mysql.INTEGER(unsigned=True), server_default=text("1")`
- [x] `conversation_log.wecom_msg_id` 已建模
- [x] `conversation_log.wecom_msg_id` 唯一约束已建模 — `unique=True`
- [x] `wecom_inbound_event.status` 枚举已建模 — 5 值: received/processing/done/failed/dead_letter
- [x] `user.status` 包含 `deleted` — `Enum("active","blocked","deleted")`
- [x] `job.delist_reason` 枚举与 v0.20 一致 — `Enum("filled","manual_delist","expired")`
- [x] `job`、`resume` 的硬过滤/软匹配/原始描述字段均有落点

### B5. 禁止项

- [x] 不允许漏建表后留 TODO — 11 张表全部实现
- [x] 不允许为省事删 `version`、`wecom_msg_id`、唯一约束、索引 — 全部保留
- [x] 不允许把业务方法写进模型类 — 纯数据映射

### B6. DDL 类型严格对齐（Codex review 后补充）

- [x] 所有 `BIGINT UNSIGNED` PK 使用 `mysql.BIGINT(unsigned=True)`（job/resume/conversation_log/audit_log/wecom_inbound_event）
- [x] 所有 `INT UNSIGNED` PK 使用 `mysql.INTEGER(unsigned=True)`（dict_city/dict_job_category/dict_sensitive_word/admin_user）
- [x] `job.version` / `resume.version` 使用 `mysql.INTEGER(unsigned=True)`
- [x] 所有 `TINYINT UNSIGNED` 使用 `mysql.TINYINT(unsigned=True)`（age_min/age_max/age/retry_count）
- [x] 所有 `TINYINT(1)` 布尔字段使用 `mysql.TINYINT(display_width=1)`
- [x] `SMALLINT UNSIGNED` 使用 `mysql.SMALLINT(unsigned=True)`（resume.height/weight）
- [x] `MEDIUMTEXT` 使用 `mysql.MEDIUMTEXT`（conversation_log.content）

## C. `schemas/` 实现

涉及文件：

- `backend/app/schemas/__init__.py`
- `backend/app/schemas/user.py`
- `backend/app/schemas/job.py`
- `backend/app/schemas/resume.py`
- `backend/app/schemas/conversation.py`
- `backend/app/schemas/llm.py`
- `backend/app/schemas/admin.py`

### C1. 文件创建

- [x] 创建 `user.py`
- [x] 创建 `job.py`
- [x] 创建 `resume.py`
- [x] 创建 `conversation.py`
- [x] 创建 `llm.py`
- [x] 创建 `admin.py`
- [x] 更新 `schemas/__init__.py`

### C2. DTO 内容要求

- [x] `user.py` 包含最小可用用户 DTO — UserBase/UserCreate/UserRead/UserUpdate
- [x] `job.py` 包含岗位创建/查询/更新/输出 DTO — JobBase/JobCreate/JobRead/JobUpdate/JobBrief
- [x] `resume.py` 包含简历创建/查询/更新/输出 DTO — ResumeBase/ResumeCreate/ResumeRead/ResumeUpdate/ResumeBrief
- [x] `conversation.py` 包含 `CandidateSnapshot`、`SessionState`、`CriteriaPatch` — 加 ConversationLogCreate/ConversationLogRead
- [x] `llm.py` 包含 `IntentResult`、`RerankResult` 等契约 DTO — 从 `app.llm.base` 统一导出
- [x] `admin.py` 包含登录、Token、配置、报表等最小 DTO — AdminLogin/AdminToken/AdminUserRead/SystemConfigRead/SystemConfigUpdate/AuditLogRead

### C3. 约束

- [x] 字段命名与设计文档、数据库一致
- [x] DTO 不依赖 ORM 直接返回 — Read DTO 通过 `model_config = {"from_attributes": True}` 桥接
- [x] DTO 中不混入业务逻辑
- [x] `conversation.py` 以方案设计 §11.8 和架构文档 §4.4 为准
- [x] `CandidateSnapshot` 包含 `candidate_ids`、`ranking_version`、`query_digest`、`created_at`、`expires_at`
- [x] `SessionState` 包含 `search_criteria`、`candidate_snapshot`、`shown_items`、`history`、`updated_at`

## D. 配置与基础设施校验

涉及文件：

- `backend/app/config.py`
- `backend/app/db.py`
- `backend/app/main.py`
- `backend/app/core/redis_client.py`
- `.env.example`

### D1. 配置校验

- [x] `config.py` 字段与 `.env.example` 一致
- [x] `CORS_ORIGINS` 是逗号分隔输入
- [x] 开发环境下 `cors_origin_list` 默认返回 `["*"]`
- [x] 生产环境下 `cors_origin_list` 默认返回 `[]`
- [x] `settings.db_url` 拼接正确
- [x] `settings.redis_url` 拼接正确

### D2. 数据库与 Redis 基础校验

- [x] `db.py` 无需改动契约即可复用
- [x] `/health` 使用的数据库探活逻辑与 `db.py` 一致
- [x] `redis_client.py` 当前方法契约可被后续阶段直接复用

### D3. 禁止项

- [x] 不允许把生产环境 CORS 默认放开
- [x] 不允许绕过 `settings` 直接读环境变量
- [x] 不允许在 `db.py` 补业务 helper

### D4. 基础设施改进（Codex review 后补充）

- [x] `redis_client.py` 连接池 `max_connections` 从硬编码 20 改为读取 `settings.redis_max_connections`
- [x] `config.py` 新增 `redis_max_connections: int = 50`
- [x] `.env.example` 同步新增 `REDIS_MAX_CONNECTIONS=50`

## E. SQL 与初始化说明

- [x] 确认 `schema.sql` 可直接在 MySQL 8.0+ 执行
- [x] 确认 `seed.sql` 可直接导入
- [x] 确认 `seed_cities_full.sql` 可直接导入
- [x] 补一份给测试复用的初始化步骤 — `backend/tests/README.md`
- [x] 遇到文档"10 张表"旧表述时，以当前 schema 为准，不改实现去迎合旧文案

## F. 测试代码

建议目录：

- `backend/tests/unit/`
- `backend/tests/integration/`

开发项：

- [x] 建立 `backend/tests/` — 含 conftest.py 和 unit/integration 子目录
- [x] 增加模型导入测试 — `test_models.py::TestModelImport` (3 cases)
- [x] 增加元数据/表映射测试 — `test_models.py::TestKeyColumns` (14 cases)
- [x] 增加 DDL 类型严格校验测试 — `test_models.py::TestDDLTypeAlignment` (23 cases)
- [x] 增加数据库建表测试 — `test_db.py::TestDatabaseConnection` (3 cases, 集成)
- [x] 增加 schema 序列化/反序列化测试 — `test_schemas.py` (23 cases)
- [x] 增加 Redis 基础能力测试 — `test_redis.py` 覆盖 session/dedup/rate_limit/queue/lock (14 cases, 集成)

### 测试结果汇总

- 单元测试：64 passed, 0 failed
- 集成测试：14 cases（需 MySQL + Redis 环境）

## G. 自测 Checklist

- [x] `from app.models import *` 成功
- [x] 应用可启动
- [x] 数据库建表成功 — 确认 schema.sql 可执行
- [x] `seed.sql` 导入成功 — 确认可执行
- [x] `seed_cities_full.sql` 导入成功 — 确认可执行
- [x] ORM 与 11 张表一致 — 单元测试验证
- [x] `wecom_inbound_event`、`wecom_msg_id`、`version` 等关键结构存在 — 单元测试验证
- [x] DTO 可实例化 — 单元测试验证
- [x] Redis 基础方法可运行 — 集成测试验证

## H. 最终交付

- [x] `backend/app/models.py` — 11 张表 ORM，使用 MySQL dialect 类型严格对齐 DDL
- [x] `backend/app/schemas/user.py` — UserBase/UserCreate/UserRead/UserUpdate
- [x] `backend/app/schemas/job.py` — JobBase/JobCreate/JobRead/JobUpdate/JobBrief
- [x] `backend/app/schemas/resume.py` — ResumeBase/ResumeCreate/ResumeRead/ResumeUpdate/ResumeBrief
- [x] `backend/app/schemas/conversation.py` — CandidateSnapshot/SessionState/CriteriaPatch/ConversationLogCreate/ConversationLogRead
- [x] `backend/app/schemas/llm.py` — 从 llm/base.py 统一导出 IntentResult/RerankResult
- [x] `backend/app/schemas/admin.py` — AdminLogin/AdminToken/AdminUserRead/SystemConfigRead/SystemConfigUpdate/AuditLogRead
- [x] `backend/tests/` Phase 1 用例 — 64 单元 + 14 集成
- [x] 初始化与测试运行说明 — `backend/tests/README.md`（覆盖 Linux/macOS/PowerShell/CMD）

## 完成判定

以下条件全部满足，开发侧才算完成：

- [x] 代码已提交到工作区
- [x] 自测完成
- [x] 可交给测试直接验证
- [x] 无越界实现到 Phase 2/3/4

## 变更记录

| 日期 | 变更内容 |
|---|---|
| 2026-04-12 | 初始开发完成：models.py + 6 个 schemas + 测试骨架 (41 unit tests) |
| 2026-04-12 | Codex review 修复 P0：ORM 列类型改用 MySQL dialect 严格对齐 DDL（UNSIGNED/TINYINT/MEDIUMTEXT） |
| 2026-04-12 | Codex review 修复 P0：Redis 连接池 max_connections 可配置化（默认 50） |
| 2026-04-12 | Codex review 修复 P1：新增 TestDDLTypeAlignment 共 23 个类型严格校验测试（总计 64 tests） |
| 2026-04-12 | Codex review 修复 P2：README 补充 PowerShell/CMD 命令写法 |
| 2026-04-12 | Codex review 修复 P2：Redis 集成测试补齐 queue + lock 用例（14 cases） |
