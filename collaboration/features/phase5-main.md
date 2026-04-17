# Feature: Phase 5 运营后台后端接口

> 状态：`draft`
> 创建日期：2026-04-16
> 对应实施阶段：Phase 5
> 关联实施文档：`docs/implementation-plan.md` §4.6
> 关联方案设计章节：§6、§7、§9、§13（全章）、§17.1（验收指标）、§17.2（命令）
> 关联架构章节：`docs/architecture.md` §二、§三、§4.5、§七（含 §7.4 Admin API 清单）
> 配套文档：
> - 开发实施文档：`collaboration/features/phase5-dev-implementation.md`
> - 开发 Checklist：`collaboration/features/phase5-dev-checklist.md`
> - 测试实施文档：`collaboration/features/phase5-test-implementation.md`
> - 测试 Checklist：`collaboration/features/phase5-test-checklist.md`

## 1. 阶段目标

Phase 5 的目标，是为运营人员日常的"审核 + 配置 + 账号 + 字典 + 报表 + 日志"工作，提供一组**可独立使用**的 `/admin/*` 后端接口，并暴露 `/api/events/*` 作为外部回传入口。

本阶段完成后，项目至少应具备以下能力：

- 运营管理员可通过 `POST /admin/login` 登录并获得 JWT，所有 `/admin/*` 接口在统一鉴权中间件下生效
- 审核工作台可支撑单人审核闭环：列表 / 详情 / 软锁 / 乐观锁 / 通过 / 驳回 / 编辑 / Undo
- 厂家、中介、工人、黑名单四类账号管理 API 可用，可完成预注册、Excel 批量导入、封禁/解封
- 岗位、简历的列表 / 详情 / 编辑 / 下架 / 延期接口可用
- 城市、工种、敏感词三本字典可读写、可批量导入
- 系统配置可分组读取 + 单项更新，并即时影响业务行为（如限流参数）
- 数据看板提供 Dashboard 概览 + 趋势 + TOP + 漏斗 + CSV 导出
- 对话日志可按 userid + 时间范围查询并导出
- 小程序点击事件回传接口 `/api/events/miniprogram_click` 可用，含 API Key 鉴权与 10 分钟去重
- Swagger UI 自动生成的 `/docs` 中所有 `/admin/*` API 可直接调通
- 所有接口统一响应格式（`code/message/data`）、统一错误码范围、统一分页结构

## 2. 当前代码现状

当前仓库内与 Phase 5 直接相关的基础已经具备：

- `backend/app/models.py`：11 张表 ORM 已完成（Phase 1 交付）
- `backend/app/schemas/admin.py`：已有 `AdminLogin / AdminToken / AdminUserRead / SystemConfigRead / SystemConfigUpdate / AuditLogRead`
- `backend/app/services/`：7 个核心业务 service（Phase 3 交付）+ `worker.py / message_router.py / command_service.py`（Phase 4 交付）
- `backend/app/core/redis_client.py`：含 session、分布式锁、限流、幂等、队列、配置缓存基础
- `backend/app/core/pagination.py`：通用分页工具
- `backend/app/core/exceptions.py`：业务异常基类
- `backend/app/api/webhook.py`：Phase 4 已交付
- `backend/app/main.py`：已注册 `webhook_router`，未注册 `admin_router` 与 `events_router`
- `backend/app/config.py`：已含 `admin_jwt_secret` / `admin_jwt_expires_hours` 配置项
- `backend/sql/seed.sql`：已写入默认管理员 `admin/admin123`、25 项默认 system_config、工种 / 城市 / 敏感词种子

当前缺失的部分：

- `backend/app/api/admin/` 目录目前只有 `__init__.py`，没有任何子模块
- `backend/app/api/events.py` 不存在
- `backend/app/api/deps.py` 不存在（`get_db / get_current_admin / get_redis` 共享依赖未集中）
- `backend/app/services/report_service.py` 不存在（架构 §二有定义但 Phase 3 未交付）
- `backend/app/services/event_service.py`（暂未存在）—— 用于事件回传去重和写库
- 没有 `event_log` 表：方案设计 §17.1.3 与架构 §7.4 都引用，但 `models.py` 与 `schema.sql` 当前未定义
- 没有任何 admin schema：`audit / accounts / jobs / resumes / dicts / config / reports / logs / events` 模块的 Pydantic DTO 全部缺失
- 没有 JWT 工具模块（`core/security.py` 或类似）
- 没有 bcrypt 密码校验 / 修改密码工具
- 没有运营操作日志写入封装（`audit_log` 仅由 Phase 3 `audit_service` 写过）
- 没有审核软锁、乐观锁、Undo 的 service 层实现
- 没有 Excel 批量导入支持（`openpyxl` 或 `pandas` 未引入）
- 没有 CSV 导出工具
- 没有 `requirements.txt` 中 `python-jose` / `passlib[bcrypt]` / `openpyxl` 依赖（需补）

## 3. 本阶段范围

### 3.1 本阶段必须完成

本阶段总体上以"接口契约 + Service 编排"为核心，前端联调由 Phase 6 负责。模块划分如下：

#### 模块 A：基础设施与共享依赖

- `backend/app/core/security.py`（新建）
  - `hash_password(plain) -> str`、`verify_password(plain, hashed) -> bool`（bcrypt）
  - `create_admin_token(admin_id, username) -> (token, expires_at)`
  - `decode_admin_token(token) -> AdminClaims`
- `backend/app/api/deps.py`（新建）
  - `get_db()` 依赖
  - `get_redis()` 依赖
  - `get_current_admin(token=Depends(oauth2_scheme), db=Depends(get_db)) -> AdminUser`
  - `require_admin` —— 鉴权中间件依赖
  - `get_event_api_key()` —— 事件回传 API Key 校验
- `backend/app/core/responses.py`（新建）
  - `ok(data) / fail(code, message) / paged(items, total, page, size)` 三个统一响应构造器
  - 包装为 `JSONResponse`，与架构 §7.3.2 完全对齐

#### 模块 B：登录与鉴权

- `backend/app/api/admin/__init__.py`：汇总 `admin_router`
- `backend/app/api/admin/auth.py`
  - `POST /admin/login` → 校验账号密码 → 返回 JWT + `password_changed` 标志
  - `GET /admin/me` → 返回当前管理员
  - `PUT /admin/me/password` → 校验旧密码 → bcrypt 哈希新密码 → 写 DB → `password_changed=1`
  - 错三次延迟 1 秒（基于 Redis 计数：`admin_login_fail:{username}`，TTL=60s）
- `backend/app/services/admin_user_service.py`（新建）
  - 用户名查询、密码校验、首次改密判定、登录失败计数

#### 模块 C：审核工作台

- `backend/app/api/admin/audit.py`
  - `GET /admin/audit/queue?status=pending|passed|rejected&target_type=&page=&size=`
  - `GET /admin/audit/{target_type}/{id}` → 详情（含 LLM 抽取、置信度、风险等级、提交者 7 天审核历史、`version` 字段）
  - `POST /admin/audit/{target_type}/{id}/lock` → Redis `SETNX audit_lock:{target_type}:{id} {operator}` TTL=300s
  - `POST /admin/audit/{target_type}/{id}/unlock` → 释放锁（仅当前持有者可释放）
  - `POST /admin/audit/{target_type}/{id}/pass` → 带 `version` 字段做乐观锁校验
  - `POST /admin/audit/{target_type}/{id}/reject` → 请求体 `{reason, notify, block_user, version}`
  - `PUT /admin/audit/{target_type}/{id}/edit` → 修正字段（带 `version`）
  - `POST /admin/audit/{target_type}/{id}/undo` → 仅在 30 秒内有效
  - `GET /admin/audit/pending-count` → 待审数量（侧边栏 badge）
- `backend/app/services/audit_workbench_service.py`（新建）
  - 软锁实现（Redis）
  - 乐观锁校验（数据库 `version` 字段，`UPDATE ... WHERE version = old_version`）
  - Undo 暂存（Redis `undo_action:{target_type}:{id}` TTL=30s，存动作前快照）
  - `audit_log` 写入（含 `operator`、`snapshot`、`reason`）
  - 7 天审核历史聚合
  - 调用 Phase 3 `audit_service` 已有的敏感词扫描以输出"风险等级"字段

#### 模块 D：账号管理

- `backend/app/api/admin/accounts.py`
  - 厂家：`GET / POST /admin/accounts/factories`、`GET / PUT /admin/accounts/factories/{userid}`、`POST /admin/accounts/factories/import`
  - 中介：同厂家，多 `can_search_jobs / can_search_workers` 字段
  - 工人：`GET /admin/accounts/workers` 仅只读 + 列表
  - 黑名单：`GET /admin/accounts/blacklist`、`POST /admin/accounts/{userid}/block`（必填 reason）、`POST /admin/accounts/{userid}/unblock`
- `backend/app/services/account_service.py`（新建）
  - 预注册（厂家/中介）：写 `user` 表，`status=active`
  - Excel 批量导入：解析、校验、批量写入、返回成功失败明细
  - 封禁/解封：写 `user.status` + `user.blocked_reason` + `audit_log`
  - 工人列表查询：分页 + 筛选

#### 模块 E：岗位与简历管理

- `backend/app/api/admin/jobs.py`
  - `GET /admin/jobs?city=&job_category=&audit_status=&days_remaining=&page=&size=&sort=`
  - `GET /admin/jobs/{id}` → 详情
  - `PUT /admin/jobs/{id}` → 编辑（带 `version` 乐观锁）
  - `POST /admin/jobs/{id}/delist` → `{reason: "manual_delist"|"filled"}`
  - `POST /admin/jobs/{id}/extend` → `{days: 15|30}`
  - `POST /admin/jobs/{id}/restore` → 取消下架（撤销 `delist_reason`）—— 仅在未到 expires_at 时允许
  - `GET /admin/jobs/export?format=csv&...筛选参数` → 导出 CSV
- `backend/app/api/admin/resumes.py`
  - 结构与岗位管理一致，筛选维度换为 `gender / age / expected_cities`
- 复用 Phase 3 的 service 层：上层 admin 接口仅做 DTO 转换、权限校验、写 `audit_log`

#### 模块 F：字典管理

- `backend/app/api/admin/dicts.py`
  - 城市：`GET / PUT /admin/dicts/cities/{id}`，`PUT` 仅允许编辑 `aliases`
  - 工种：`GET / POST / PUT / DELETE /admin/dicts/job-categories[/{id}]`
  - 敏感词：`GET / POST / DELETE /admin/dicts/sensitive-words[/{id}]`
  - 敏感词批量：`POST /admin/dicts/sensitive-words/batch` → `{words: ["w1","w2"], level: "high|mid|low", category}`
- `backend/app/services/dict_service.py`（新建）
  - CRUD + 校验 + 唯一性检查
  - 敏感词批量去重、按等级写入

#### 模块 G：系统配置

- `backend/app/api/admin/config.py`
  - `GET /admin/config` → 全部配置项，按 `prefix.before_first_dot`（如 `ttl / match / filter / audit / llm / session / upload / report / rate_limit`）分组
  - `PUT /admin/config/{key}` → 更新单项 `{value, value_type?}`，写入 `system_config.updated_by`
  - 危险项变更（`filter.* / llm.provider`）必须写 `audit_log`
- `backend/app/services/system_config_service.py`（新建或扩充已有 `core/redis_client.py` 配置缓存逻辑）
  - 单项读取 / 全部读取 / 单项更新
  - 缓存失效（更新后清除 `config_cache:{key}`）
  - 类型校验（int/bool/json/string）

#### 模块 H：数据看板

- `backend/app/api/admin/reports.py`
  - `GET /admin/reports/dashboard` → 当日核心指标（DAU、上传数、检索次数、命中率、空召回率、待审积压、昨日对比）
  - `GET /admin/reports/trends?range=7d|30d|custom&from=&to=` → 趋势数据（每日 / 角色拆分）
  - `GET /admin/reports/top?dim=city|job_category|role&limit=10`
  - `GET /admin/reports/funnel` → 转化漏斗（注册 → 首次检索 → 推荐 → 详情点击）
  - `GET /admin/reports/export?metric=xxx&from=&to=&format=csv`
- `backend/app/services/report_service.py`（新建）
  - 复用对话日志、检索日志、`event_log`、`audit_log` 计算指标
  - 缓存 60 秒（Redis `report_cache:dashboard:{date}`）
- 一期所有指标按"近 30 天"聚合，无需引入 BI 工具

#### 模块 I：对话日志查询

- `backend/app/api/admin/logs.py`
  - `GET /admin/logs/conversations?userid=&start=&end=&direction=&intent=&page=&size=`
  - `GET /admin/logs/conversations/export?...筛选参数`
- 仅按 userid + 时间范围 + 方向 + intent 过滤，**不做全文检索**（v1 边界）

#### 模块 J：事件回传 API

- `backend/app/api/events.py`
  - `POST /api/events/miniprogram_click` → 接收 `{userid, target_type, target_id, timestamp}`
  - 鉴权：`X-Event-Api-Key` Header，从 `.env` 配置 `event_api_key`，**不走 JWT**
  - 幂等：同一 `(userid, target_type, target_id)` 10 分钟内去重，Redis `event_idem:{...}` SETNX TTL=600
  - 写入 `event_log` 表（本阶段需新增）
- `backend/app/services/event_service.py`（新建）
  - 校验 + 幂等 + 写库
  - 失败时写 `audit_log`（防止丢点击）

#### 模块 K：基础设施补强

- `backend/sql/schema.sql`：新增 `event_log` 表（与 §7.4 引用对齐），`models.py` 同步新增
  ```
  CREATE TABLE event_log (
    id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
    event_type ENUM('miniprogram_click') NOT NULL,
    userid VARCHAR(64) NOT NULL,
    target_type ENUM('job','resume') NOT NULL,
    target_id BIGINT UNSIGNED NOT NULL,
    occurred_at DATETIME NOT NULL,
    extra JSON NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_target (target_type, target_id, occurred_at),
    INDEX idx_user_time (userid, occurred_at)
  );
  ```
- `backend/sql/seed.sql`：新增 `event.api_key` 默认（如有需要）/ 提示 `.env` 配置
- `backend/app/main.py`：注册 `admin_router` 与 `events_router`
- `backend/requirements.txt`：补 `python-jose[cryptography]`、`passlib[bcrypt]`、`openpyxl`、`bcrypt`（如缺失）

#### 模块 L：响应格式与异常处理

- `backend/app/main.py` 注册全局异常处理器：
  - `BusinessException` → `{"code": <错误码>, "message": <提示>, "data": null}`
  - `RequestValidationError` → `{"code": 40100, "message": "参数错误", "data": {"fields": ...}}`
  - `HTTPException` → 标准化包装
- 错误码区段固化（架构 §7.3.3）：
  - `0` 成功
  - `40001-40099` 鉴权（含 `40001` 用户名/密码错误、`40002` token 过期、`40003` token 无效）
  - `40100-40199` 参数错误
  - `40300-40399` 权限不足
  - `40400-40499` 资源不存在
  - `40900-40999` 业务冲突（含 `40901` 软锁冲突、`40902` 乐观锁冲突）
  - `50000-50099` 内部错误
  - `50100-50199` LLM 异常

### 3.2 本阶段明确不做

- 不实现前端页面（Phase 6 范围）
- 不实现 RBAC / 多管理员账号 / 找回密码（v1 边界，§13.3）
- 不实现告警规则配置 / 自定义看板 / BI（v1 边界）
- 不实现对话日志全文检索（v1 边界）
- 不实现 prompt 热更新（v1 边界）
- 不实现审核员绩效统计（v1 边界）
- 不在本阶段做 nginx / TLS / 公网联调（Phase 7 范围）
- 不在本阶段做定时任务（TTL 清理、日报推送）（Phase 7 范围）
- 不修改 Phase 3/4 service 层业务逻辑（仅在缺口处补 admin 专用方法）
- 不引入向量数据库 / RAG
- 不实现 prompt 在 admin 中的可视化编辑
- 不允许 admin 接口直接拼裸 SQL，必须经 service 层
- 不允许 admin 接口直接返回 ORM 对象，必须经 schema DTO

## 4. 真值来源与实现基线

出现冲突时，按以下优先级执行：

1. `docs/implementation-plan.md` §4.6
2. `方案设计_v0.1.md` §13（运营后台）、§9.3（封禁）、§17.1（指标）、§17.2（命令）
3. `docs/architecture.md` §二、§4.5、§7.3、§7.4
4. `collaboration/architecture/backend.md`
5. `collaboration/features/phase4-main.md`（Phase 4 handoff）
6. 本文档

本阶段额外锁定以下实现约束：

- 所有 `/admin/*` 接口必须经过 `Depends(get_current_admin)` 鉴权，**没有公开端点**
- 所有写操作（编辑、封禁、解封、下架、延期、字典 CRUD、配置更新、审核动作）必须写 `audit_log`，且 `operator` 字段写当前管理员 username
- 所有列表接口必须支持分页（`page` + `size`，默认 `page=1, size=20, max size=100`）和导出（CSV）
- 所有列表筛选参数必须明确白名单，不允许把任意字段传进去做 `WHERE`
- 所有更新接口（除字典/配置）必须带 `version` 字段做乐观锁校验
- `audit_log.snapshot` 仅记录变更前的关键字段，不存全表
- 软锁、Undo 全部走 Redis，TTL 严格遵守 300s / 30s
- `event_log` 写入失败必须有日志兜底，**不能阻塞业务回包**
- 系统配置的更新必须立即影响业务（清缓存）
- 危险配置项（`filter.* / llm.provider`）改动必须额外提示并写 `audit_log`

### 4.1 Phase 5 依赖的 `system_config` key 与 `.env` 配置

| key | 用途 | 默认值/来源 |
|---|---|---|
| `event.dedupe_window_seconds` | 事件回传去重窗口（秒） | 默认 600，需在 seed.sql 新增 |
| `audit.lock_ttl_seconds` | 审核软锁 TTL | 默认 300，新增 |
| `audit.undo_window_seconds` | Undo 窗口 TTL | 默认 30，新增 |
| `report.cache_ttl_seconds` | Dashboard 缓存 TTL | 默认 60，新增 |
| `account.import_max_rows` | Excel 导入单批最大行数 | 默认 500，新增 |

`.env` 新增：

| key | 说明 |
|---|---|
| `ADMIN_JWT_SECRET` | JWT 签名密钥（已有，需在生产前替换） |
| `ADMIN_JWT_EXPIRES_HOURS` | JWT 过期小时数（已有，默认 24） |
| `EVENT_API_KEY` | 事件回传 API Key（新增） |

## 5. 详细需求说明

### 5.1 鉴权与登录

- JWT 载荷：`{"sub": <admin_user.id>, "username": <username>, "exp": <timestamp>}`
- 登录失败计数：连续 3 次失败后服务端返回 `40001` 之外，额外 sleep 1 秒（防暴力）
- 首次登录（`password_changed=0`）允许继续访问，但前端会按返回的标志强制跳转改密码页
- `PUT /admin/me/password`：必须带旧密码；新密码长度 ≥ 8；与旧密码不同；成功后 `password_changed=1`，旧 token 仍然有效（不强制下线）

### 5.2 审核工作台

- 软锁键：`audit_lock:{target_type}:{id}`（值=当前 operator）
- 乐观锁错误码：`40902`，前端提示"此条目已被修改，请刷新"
- 软锁冲突错误码：`40901`，返回 `data: {locked_by: "<其他管理员>"}`
- Undo 数据结构：`{action, target_type, target_id, before_snapshot, after_snapshot, version_before, operator, ts}`
- Undo 仅允许撤销最近一个动作；undo 之后该 key 立即删除
- `pass / reject / edit` 动作均写 `audit_log`，`action` 字段对应 `manual_pass / manual_reject / manual_edit`（`manual_edit` 已在枚举里？需确认 —— 当前 `audit_action` 枚举为 `auto_pass / auto_reject / manual_pass / manual_reject / appeal / reinstate`，**需要 Phase 5 在 schema 层新增 `manual_edit / undo`**，并同步 `models.py` 与 `sql/schema.sql`）
- 详情接口必须返回：
  ```json
  {
    "id": 1, "target_type": "job", "version": 3,
    "owner_userid": "...", "raw_text": "...",
    "extracted_fields": {...},
    "field_confidence": {"city": 0.95, "salary": 0.42, ...},
    "risk_level": "low|mid|high",
    "trigger_rules": ["敏感词:传销"],
    "submitter_history": {"passed": 5, "rejected": 1, "last_7d": [...]},
    "locked_by": null,
    "audit_status": "pending"
  }
  ```

### 5.3 账号管理

- 预注册请求字段：`{role, display_name, company?, contact_person?, phone, can_search_jobs?, can_search_workers?}`
- `external_userid` 由后端生成（如 `pre_<role>_<uuid8>`），等用户首次绑定企微后通过自助流程或运维操作改为真实 external_userid（一期可允许 admin 编辑该字段）
- Excel 批量导入：
  - 模板列：`role, display_name, company, contact_person, phone, can_search_jobs, can_search_workers, external_userid?`
  - 单批不超过 `account.import_max_rows`
  - 全量校验后再写入；任意一行错误则全部回滚（事务）
  - 返回 `{success_count, failed: [{row, error}]}`
- 封禁请求：`{reason, notify_user?: bool}` —— `notify_user=true` 时本期仍然只是预留字段，不发推送（消息推送逻辑见 Phase 7）

### 5.4 岗位 / 简历管理

- 列表筛选白名单：
  - 岗位：`city / district / job_category / pay_type / audit_status / delist_reason / owner_userid / created_from / created_to / expires_from / expires_to / salary_min / salary_max`
  - 简历：`gender / age_min / age_max / expected_cities / expected_job_categories / audit_status / owner_userid / created_from / created_to`
- 编辑接口：仅允许编辑业务字段，不允许编辑 `id / owner_userid / created_at / version` —— `version` 由后端递增
- `extend`：`days ∈ {15, 30}`，`expires_at = max(now, expires_at) + days`，但不超过 `ttl.job.days * 2`（与 Phase 4 续期一致）
- `restore`：仅当 `delist_reason IS NOT NULL` 且 `expires_at > now()` 时允许；置空 `delist_reason`
- 导出 CSV：默认导出当前筛选结果的全部字段（剥离 `extra.raw_salary_text` 等内部字段），文件名 `jobs_{yyyyMMddHHmm}.csv`，UTF-8 BOM 头以兼容 Excel

### 5.5 字典管理

- 城市：列表按省份分组返回（`{province, items: [...]}`），仅允许编辑 `aliases`
- 工种：CRUD + 拖拽排序（前端通过 `PUT /admin/dicts/job-categories/{id}` 传 `sort_order` 实现）
- 敏感词：CRUD + 批量导入；批量导入需返回新增数量与重复数量
- 字典任意变更必须清空对应缓存（如有，调用 service 层方法）

### 5.6 系统配置

- 全部配置按 key 第一段（`ttl / match / filter / audit / llm / session / upload / report / rate_limit / event / account`）分组返回
- 单项更新必须按 `value_type` 校验：
  - `int` → 整数
  - `bool` → "true" / "false"
  - `json` → 可解析 JSON
  - `string` → 任意字符串
- 危险项白名单：`filter.enable_gender / filter.enable_age / filter.enable_ethnicity / llm.provider`
  - 修改时必须返回提示文案 `"该配置变更将立即影响业务，请确认"`
  - 写 `audit_log`，`action=manual_edit`，`target_type='system'`、`target_id=<config_key>`、`snapshot={old, new}`
- 更新后清除 Redis `config_cache:{key}` 与全量缓存 `config_cache:all`

### 5.7 数据看板

`GET /admin/reports/dashboard` 返回结构：

```json
{
  "today": {
    "dau_total": 120, "dau_worker": 80, "dau_factory": 25, "dau_broker": 15,
    "uploads_job": 8, "uploads_resume": 12,
    "search_count": 230, "hit_rate": 0.78,
    "empty_recall_rate": 0.18,
    "audit_pending": 14
  },
  "yesterday": { ... 同结构 },
  "trend_7d": [{"date":"2026-04-10","dau":..., ...}, ...]
}
```

`GET /admin/reports/trends`：返回多个指标系列；前端 ECharts 直接消费

`GET /admin/reports/funnel`：返回 `[{stage, count}]`，阶段固定为：注册 → 首次发消息 → 首次有效检索 → 收到推荐 → 点详情

### 5.8 对话日志

- 必传至少一个 `userid` 或 `external_userid_hash` —— 防止全量拉表
- 时间范围最大 30 天
- 列表按时间倒序，分页默认 `size=50, max=200`
- 导出 CSV 含 `criteria_snapshot` 序列化结果

### 5.9 事件回传 API

- 路径：`POST /api/events/miniprogram_click`
- 请求体：`{userid: str, target_type: "job"|"resume", target_id: int, timestamp: int(可选, 默认 now)}`
- 鉴权：`X-Event-Api-Key` 必须等于 `.env` 中 `EVENT_API_KEY`，否则返回 `40001`
- 幂等：`SETNX event_idem:{userid}:{target_type}:{target_id} 1 EX 600`，已存在则直接返回 `code=0` 但不重复写库
- 响应：`{"code": 0, "message": "ok", "data": {"deduped": true|false}}`
- 写入 `event_log` 失败 → 写 `audit_log` `action='auto_reject'`、`target_type='user'`、`target_id=userid`、`reason='event_log write failed'`

## 6. 接口契约

### 6.1 全量 API 清单（覆盖 50 个端点，对齐架构 §7.4）

| 模块 | 方法 | 路径 | 说明 |
|---|---|---|---|
| 鉴权 | POST | `/admin/login` | 登录 |
| 鉴权 | GET  | `/admin/me` | 当前用户 |
| 鉴权 | PUT  | `/admin/me/password` | 改密码 |
| 审核 | GET  | `/admin/audit/queue` | 待审队列（分页/筛选） |
| 审核 | GET  | `/admin/audit/pending-count` | 待审数量 |
| 审核 | GET  | `/admin/audit/{type}/{id}` | 详情 |
| 审核 | POST | `/admin/audit/{type}/{id}/lock` | 软锁 |
| 审核 | POST | `/admin/audit/{type}/{id}/unlock` | 释放软锁 |
| 审核 | POST | `/admin/audit/{type}/{id}/pass` | 通过 |
| 审核 | POST | `/admin/audit/{type}/{id}/reject` | 驳回 |
| 审核 | PUT  | `/admin/audit/{type}/{id}/edit` | 编辑修正 |
| 审核 | POST | `/admin/audit/{type}/{id}/undo` | 撤销 |
| 厂家 | GET/POST | `/admin/accounts/factories` | 列表 / 预注册 |
| 厂家 | GET/PUT  | `/admin/accounts/factories/{userid}` | 详情 / 编辑 |
| 厂家 | POST | `/admin/accounts/factories/import` | Excel 批量导入 |
| 中介 | GET/POST | `/admin/accounts/brokers` | 同厂家 |
| 中介 | GET/PUT  | `/admin/accounts/brokers/{userid}` | 同上 |
| 中介 | POST | `/admin/accounts/brokers/import` | 同上 |
| 工人 | GET  | `/admin/accounts/workers` | 列表只读 |
| 工人 | GET  | `/admin/accounts/workers/{userid}` | 详情只读 |
| 黑名单 | GET | `/admin/accounts/blacklist` | 列表 |
| 黑名单 | POST | `/admin/accounts/{userid}/block` | 封禁 |
| 黑名单 | POST | `/admin/accounts/{userid}/unblock` | 解封 |
| 岗位 | GET  | `/admin/jobs` | 列表 |
| 岗位 | GET/PUT  | `/admin/jobs/{id}` | 详情 / 编辑 |
| 岗位 | POST | `/admin/jobs/{id}/delist` | 下架 |
| 岗位 | POST | `/admin/jobs/{id}/extend` | 延期 |
| 岗位 | POST | `/admin/jobs/{id}/restore` | 取消下架 |
| 岗位 | GET  | `/admin/jobs/export` | 导出 |
| 简历 | GET  | `/admin/resumes` | 列表 |
| 简历 | GET/PUT  | `/admin/resumes/{id}` | 详情 / 编辑 |
| 简历 | POST | `/admin/resumes/{id}/delist` | 下架 |
| 简历 | POST | `/admin/resumes/{id}/extend` | 延期 |
| 简历 | GET  | `/admin/resumes/export` | 导出 |
| 城市 | GET  | `/admin/dicts/cities` | 列表 |
| 城市 | PUT  | `/admin/dicts/cities/{id}` | 编辑 alias |
| 工种 | GET/POST | `/admin/dicts/job-categories` | 列表 / 新增 |
| 工种 | PUT/DELETE | `/admin/dicts/job-categories/{id}` | 编辑 / 删除 |
| 敏感词 | GET/POST | `/admin/dicts/sensitive-words` | 列表 / 新增 |
| 敏感词 | DELETE | `/admin/dicts/sensitive-words/{id}` | 删除 |
| 敏感词 | POST | `/admin/dicts/sensitive-words/batch` | 批量导入 |
| 配置 | GET  | `/admin/config` | 全部配置 |
| 配置 | PUT  | `/admin/config/{key}` | 单项更新 |
| 看板 | GET  | `/admin/reports/dashboard` | 概览 |
| 看板 | GET  | `/admin/reports/trends` | 趋势 |
| 看板 | GET  | `/admin/reports/top` | TOP 榜单 |
| 看板 | GET  | `/admin/reports/funnel` | 漏斗 |
| 看板 | GET  | `/admin/reports/export` | 导出 |
| 日志 | GET  | `/admin/logs/conversations` | 查询 |
| 日志 | GET  | `/admin/logs/conversations/export` | 导出 |
| 事件 | POST | `/api/events/miniprogram_click` | 点击回传 |

合计 ~50 个端点。

### 6.2 统一响应格式

```json
// 成功
{"code": 0, "message": "ok", "data": {...}}

// 分页
{"code": 0, "message": "ok", "data": {"items": [...], "total": 127, "page": 1, "size": 20, "pages": 7}}

// 错误
{"code": 40001, "message": "用户名或密码错误", "data": null}
```

### 6.3 Pagination 与排序

- 分页：`?page=1&size=20`，`size ∈ [1, 100]`
- 排序：`?sort=created_at:desc,city:asc`，字段必须在白名单内

### 6.4 错误码补充（新增 / 调整）

| code | 含义 | 备注 |
|---|---|---|
| 40001 | 用户名或密码错误 | 已有 |
| 40002 | Token 过期 | 新 |
| 40003 | Token 无效 | 新 |
| 40101 | 参数错误 | 已有 |
| 40301 | 权限不足 | 已有 |
| 40401 | 资源不存在 | 已有 |
| 40901 | 软锁冲突 | 新（审核） |
| 40902 | 乐观锁冲突 | 新（审核 / 编辑） |
| 40903 | 审核动作不可撤销（超过 30s） | 新 |
| 40904 | 操作被禁用（如重复封禁） | 新 |
| 50001 | 内部错误 | 已有 |
| 50101 | LLM 异常 | 已有 |

## 7. 验收标准

- [ ] Swagger UI `/docs` 中所有 `/admin/*` API 与 `/api/events/*` 可见、可调通
- [ ] 登录可正常获取 JWT；无 token 访问任意 `/admin/*` 返回 40003
- [ ] 改密码可成功，旧密码错误返回 40001，新密码长度不足返回 40101
- [ ] 审核队列可分页 / 筛选；详情含 `version / locked_by / risk_level / submitter_history`
- [ ] 软锁有效：A 锁定后 B 打开返回 `40901 + locked_by=A`，A unlock 后 B 可进入
- [ ] 乐观锁有效：`version` 不一致时通过/驳回/编辑返回 40902
- [ ] Undo 30 秒内可撤销，超过返回 40903
- [ ] 厂家/中介可预注册并支持 Excel 批量导入（成功 / 失败明细）
- [ ] 工人列表只读
- [ ] 黑名单封禁必须填理由，封禁后用户表 `status=blocked`，写入 audit_log
- [ ] 岗位/简历列表筛选、排序、分页、导出 CSV 全部生效
- [ ] 岗位下架 / 延期 / 取消下架行为符合规范，写 audit_log
- [ ] 字典 CRUD（城市仅 alias 编辑、工种 CRUD、敏感词 CRUD + 批量）正常
- [ ] 系统配置可分组读取、单项更新；危险项变更写 audit_log 且清缓存
- [ ] 限流参数变更后 webhook 限流行为立即生效（端到端验证）
- [ ] Dashboard 当日数据 + 昨日对比 + 趋势可正常返回
- [ ] 报表 CSV 导出文件可被 Excel 直接打开（UTF-8 BOM）
- [ ] 对话日志查询必须带 userid 和时间范围；超出 30 天范围返回 40101
- [ ] `/api/events/miniprogram_click`：API Key 校验生效；幂等窗口生效；写入 event_log 表
- [ ] 所有写操作均产生 audit_log 记录（operator = 当前管理员）
- [ ] 全部接口响应格式统一（code/message/data），错误码在文档定义范围内
- [ ] `models.py` 与 `schema.sql` 同步新增 `event_log` 表 + `audit_action` 枚举扩展
- [ ] `requirements.txt` 已补 jose / passlib / openpyxl 依赖

## 8. 进入条件

| 条件 | 状态 | 说明 | 如未满足的应对 |
|---|---|---|---|
| Phase 1 数据层与 Phase 3 service 层已交付 | 待确认 | Phase 5 强依赖 service 层做业务编排 | 不满足 → Phase 5 无法开始 |
| Phase 4 webhook + worker 已交付 | 待确认 | Phase 5 与 Phase 4 并不冲突，但部分接口（限流配置生效）需 Phase 4 已实现才能联动验证 | 不满足 → Phase 5 可开发，但限流端到端验证延后 |
| 默认管理员账号 / `EVENT_API_KEY` | 待确认 | seed.sql 已含默认 admin/admin123；`EVENT_API_KEY` 需在 `.env` 设置 | 缺失 → 临时本地配置，不阻塞开发 |
| 小程序点击事件契约（target_type / target_id 字段定义） | 待确认 | 需与小程序客户端对齐 | 缺失 → 按本文档 §5.9 字段先实现，后续小程序适配 |

## 9. 风险与备注

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| `audit_action` 枚举需新增 `manual_edit / undo`，涉及 DDL 变更 | 老数据兼容、迁移脚本 | 一期数据量小，本阶段直接在 `schema.sql` 修改并提供 ALTER 语句 |
| 报表 SQL 在大表上可能慢 | Dashboard 接口可能 > 1s | 加缓存（60 秒），增量字段加索引（`audit_log.created_at` 已有） |
| Excel 批量导入可能引入恶意公式（CSV 注入） | 安全风险 | 导入前去除以 `=`、`+`、`-`、`@` 开头的字段或转义 |
| `event_api_key` 泄漏 | 数据被刷 | 一期接受，文档要求生产环境每季度轮换；幂等窗口可降低影响 |
| 软锁因 Worker 崩溃残留 | 其他人无法接管 | Lock TTL 自动到期；同时提供"强制接管"场景由前端在 §13.9.5 处理（本期接口预留） |
| 配置缓存与多实例不一致 | 改了不生效 | 更新接口主动 DEL，全部读取走 Redis；后续可加 pubsub 通知 |
| 大量列表导出会拖死 Worker | DB 压力 | 导出接口默认限制 10000 行，超出返回 40101 提示分批导出 |

## 10. 文件变更清单

| 操作 | 文件 | 说明 |
|---|---|---|
| 新建 | `backend/app/api/deps.py` | 共享依赖 |
| 新建 | `backend/app/core/security.py` | JWT + bcrypt |
| 新建 | `backend/app/core/responses.py` | 统一响应封装 |
| 新建 | `backend/app/api/admin/auth.py` | 登录 / me / 改密 |
| 新建 | `backend/app/api/admin/audit.py` | 审核工作台 |
| 新建 | `backend/app/api/admin/accounts.py` | 账号管理 |
| 新建 | `backend/app/api/admin/jobs.py` | 岗位管理 |
| 新建 | `backend/app/api/admin/resumes.py` | 简历管理 |
| 新建 | `backend/app/api/admin/dicts.py` | 字典管理 |
| 新建 | `backend/app/api/admin/config.py` | 系统配置 |
| 新建 | `backend/app/api/admin/reports.py` | 数据看板 |
| 新建 | `backend/app/api/admin/logs.py` | 对话日志 |
| 修改 | `backend/app/api/admin/__init__.py` | `admin_router` 汇总 |
| 新建 | `backend/app/api/events.py` | 事件回传 |
| 新建 | `backend/app/services/admin_user_service.py` | admin 用户 service |
| 新建 | `backend/app/services/audit_workbench_service.py` | 审核工作台 service |
| 新建 | `backend/app/services/account_service.py` | 账号 service |
| 新建 | `backend/app/services/dict_service.py` | 字典 service |
| 新建 | `backend/app/services/system_config_service.py` | 配置 service |
| 新建 | `backend/app/services/report_service.py` | 报表 service |
| 新建 | `backend/app/services/event_service.py` | 事件 service |
| 新建 | `backend/app/schemas/audit.py` | 审核 DTO |
| 新建 | `backend/app/schemas/account.py` | 账号 DTO |
| 新建 | `backend/app/schemas/dict.py` | 字典 DTO |
| 新建 | `backend/app/schemas/report.py` | 报表 DTO |
| 新建 | `backend/app/schemas/event.py` | 事件 DTO |
| 修改 | `backend/app/schemas/admin.py` | 补 ChangePassword 等 |
| 修改 | `backend/app/schemas/job.py` | 补 admin 用 DTO |
| 修改 | `backend/app/schemas/resume.py` | 补 admin 用 DTO |
| 修改 | `backend/app/models.py` | 新增 `EventLog`，扩展 `audit_action` 枚举 |
| 修改 | `backend/sql/schema.sql` | 新增 `event_log` 表，扩展 `audit_action` 枚举 |
| 修改 | `backend/sql/seed.sql` | 新增 5 个 system_config key |
| 修改 | `backend/app/main.py` | 注册 admin_router、events_router、全局异常处理 |
| 修改 | `backend/app/config.py` | 补 `event_api_key` 字段 |
| 修改 | `backend/requirements.txt` | 补 `python-jose`、`passlib[bcrypt]`、`openpyxl` |
| 可能修改 | `backend/app/core/redis_client.py` | 新增 audit_lock / undo / config_cache 工具方法 |
