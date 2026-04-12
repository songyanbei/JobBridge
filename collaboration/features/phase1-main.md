# Feature: Phase 1 数据与后端骨架

> 状态：`done`
> 创建日期：2026-04-12
> 完成日期：2026-04-12
> 对应实施阶段：Phase 1
> 关联方案设计章节：§5、§7、§11.8、§12.6、§14.2、§14.4
> 关联架构章节：§二、§三、§四、§八
> 配套清单：
> - 开发 Checklist：`collaboration/features/phase1-dev-checklist.md`
> - 测试 Checklist：`collaboration/features/phase1-test-checklist.md`

## 需求目标

在 Phase 1 内完成 JobBridge 后端基础骨架，使后续 Phase 2-4 可以直接基于稳定的数据模型、DTO、配置和基础设施继续开发，而不需要返工调整底层结构。

Phase 1 做完后，项目至少要具备以下能力：

- 数据库表结构已被 ORM 正确映射
- Pydantic DTO 已具备最小可用骨架
- 数据库与 Redis 的基础访问能力可被后续模块直接复用
- 测试同学可以独立完成本地环境准备、建表、导数、导入和基础验证

## 当前行为

当前项目已经具备：

- `backend/sql/schema.sql`
- `backend/sql/seed.sql`
- `backend/sql/seed_cities_full.sql`
- `backend/app/config.py`
- `backend/app/db.py`
- `backend/app/core/exceptions.py`
- `backend/app/core/pagination.py`
- `backend/app/core/redis_client.py`
- 包目录骨架：`schemas/`、`api/`、`services/`、`llm/`、`storage/`、`tasks/`、`wecom/`

当前项目尚未具备：

- `backend/app/models.py`
- Phase 1 所需的各个 `schemas/*.py`
- 面向 Phase 1 的自动化测试目录和用例

## Phase 1 范围

### 本阶段必须完成

1. ORM 模型
2. Pydantic DTO
3. 配置和数据库基础设施收口
4. Redis 基础工具契约确认
5. SQL 初始化和种子数据可执行
6. Phase 1 自动化测试骨架

### 本阶段明确不做

- 不做业务 service 编排
- 不做 webhook / worker / message_router
- 不做企微联调
- 不做真实 LLM 调用
- 不做 admin API
- 不做前端页面或前后端联调
- 不把 `conversation_session` 改成 MySQL 存储

## 真值来源与实现基线

出现文档不一致时，按以下优先级执行：

1. `backend/sql/schema.sql`
2. `方案设计_v0.1.md` 中最新章节和补充说明
3. `docs/architecture.md`
4. 本文档

特别说明：

- `方案设计_v0.1.md` 和 `docs/architecture.md` 中仍存在“10 张表”的旧表述。
- **Phase 1 一律以 `backend/sql/schema.sql` 当前内容为准，按 11 张 MySQL 表实现。**

## 关键需求说明

### 1. ORM 映射范围

必须映射以下 11 张表：

- `user`
- `job`
- `resume`
- `conversation_log`
- `audit_log`
- `dict_city`
- `dict_job_category`
- `dict_sensitive_word`
- `system_config`
- `admin_user`
- `wecom_inbound_event`

### 2. 必须覆盖的新增字段/结构

以下内容属于 Phase 1 必须显式覆盖项：

| 表/模块 | 字段/结构 | 说明 |
|---|---|---|
| `job` | `version` | 乐观锁版本号，后续审核工作台依赖 |
| `resume` | `version` | 乐观锁版本号，后续审核工作台依赖 |
| `conversation_log` | `wecom_msg_id` | 企微消息 ID，后续用于幂等追踪 |
| `conversation_log` | `uk_msg_id` 唯一约束 | `wecom_msg_id` 必须唯一 |
| `wecom_inbound_event` | 整表 | 后续异步消息链路状态跟踪依赖 |
| `schemas/conversation.py` | `CandidateSnapshot` | 检索快照 DTO |
| `schemas/conversation.py` | `SessionState` | Redis 会话状态 DTO |

### 3. ORM 实现约束

- ENUM 字段使用字符串类型 + `sa.Enum`
- JSON 字段使用 `sa.JSON`
- `extra` 字段使用 `MutableDict.as_mutable(sa.JSON)`
- 不在 ORM 层实现全局软删除过滤
- 所有默认值、索引、唯一约束、可空性必须与 DDL 一致

### 4. DTO 实现约束

需要创建的 DTO 文件：

- `backend/app/schemas/user.py`
- `backend/app/schemas/job.py`
- `backend/app/schemas/resume.py`
- `backend/app/schemas/conversation.py`
- `backend/app/schemas/llm.py`
- `backend/app/schemas/admin.py`

要求：

- 命名与数据库和设计文档保持一致
- 支撑后续 API / Service 使用
- 不提前混入业务逻辑
- `schemas/conversation.py` 以方案设计 §11.8 和架构文档 §4.4 为准

### 5. 配置与基础设施要求

Phase 1 只需要确认和收口，不要求重构已有基础模块，但必须确认以下内容可被后续阶段直接复用：

- `config.py`
- `db.py`
- `core/redis_client.py`

重点确认：

- `CORS_ORIGINS` 为环境变量驱动
- 开发/生产环境下 CORS 默认行为符合设计
- `db.py` 中 `Base`、`engine`、`SessionLocal`、`get_db()` 可直接复用
- `redis_client.py` 中 session、幂等、限流、队列、锁等方法契约不需要在 Phase 1 改动

## 交付物

Phase 1 完成后，后端必须交付：

- `backend/app/models.py`
- `backend/app/schemas/user.py`
- `backend/app/schemas/job.py`
- `backend/app/schemas/resume.py`
- `backend/app/schemas/conversation.py`
- `backend/app/schemas/llm.py`
- `backend/app/schemas/admin.py`
- `backend/tests/` 下的 Phase 1 自动化测试
- 一份本地初始化与测试运行说明

## 前端需要做什么

本阶段前端无开发任务。

说明：

- Phase 1 不涉及前端页面、接口联调或交互确认
- 如前端需要了解未来字段，只读取本主文档和 `schema.sql`
- 如后端发现 DTO 命名会影响后续前端契约，请在进入 Phase 5 前通过 handoff 文档同步

## 验收标准

- `backend/app/models.py` 已完成并覆盖 11 张表
- 所有 Phase 1 所需 `schemas/` 文件已创建
- `wecom_inbound_event`、`conversation_log.wecom_msg_id`、`job.version`、`resume.version` 已完整覆盖
- `config.py`、`db.py`、`core/redis_client.py` 契约已确认可复用
- 自动化测试能证明模型、DTO、数据库、Redis 基础能力正常
- 测试同学可仅凭主文档和测试 checklist 完成环境准备与需求复现

## 风险 / 备注

### 1. 文档旧表述风险

部分设计文档还保留“10 张表”的旧表述，Phase 1 以 `schema.sql` 为准，不反向修改实现去迎合旧表述。

### 2. Phase 1 与后续阶段边界

如果开发过程中出现以下行为，视为越界：

- 为了写 DTO 顺手实现 service
- 为了验证模型顺手实现 API
- 为了验证结构顺手接企微或真实 LLM

### 3. 完成后需要做的事情

- 将本文件状态更新为 `done`
- 记录实际完成日期
- 如发现会影响 Phase 2/3 的字段或契约问题，写入 handoff 文档
