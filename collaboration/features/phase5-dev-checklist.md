# Phase 5 开发 Checklist

> 基于：`collaboration/features/phase5-main.md`
> 配套实施文档：`collaboration/features/phase5-dev-implementation.md`
> 面向角色：后端开发
> 状态：`draft`
> 创建日期：2026-04-16

## A. 基线确认

- [ ] 已阅读 `collaboration/features/phase5-main.md`
- [ ] 已阅读 `collaboration/features/phase5-dev-implementation.md`
- [ ] 已确认本阶段只做 admin API + 事件回传 API + 配套 service / schema / DDL，不做前端 / 定时任务 / nginx
- [ ] 已确认所有 `/admin/*` 必须经 `require_admin` 鉴权
- [ ] 已确认所有写操作必须写 `audit_log` 且 `operator=current.username`
- [ ] 已确认所有列表 / 详情必须经 schema DTO 转换，不返回 ORM 对象
- [ ] 已确认审核动作必须走乐观锁（`version`）+ 软锁（Redis）+ Undo（30s）
- [ ] 已确认 admin 接口不允许直接拼裸 SQL，统一经 service 层
- [ ] 已确认错误码统一在 §6.4 范围内，全局异常处理器统一封装

## B. 基础设施与共享依赖

涉及文件：

- `backend/app/core/security.py`（新建）
- `backend/app/core/responses.py`（新建）
- `backend/app/api/deps.py`（新建）
- `backend/app/main.py`（修改：异常处理器、路由注册）
- `backend/requirements.txt`（修改）

### B.1 安全工具

- [ ] `core/security.py` 已创建
- [ ] `hash_password / verify_password` 基于 `passlib[bcrypt]`
- [ ] `create_admin_token / decode_admin_token` 基于 `python-jose`
- [ ] JWT 载荷含 `sub / username / exp`
- [ ] `decode_admin_token` 异常时抛 `BusinessException(40003)`

### B.2 共享依赖

- [ ] `api/deps.py` 已创建
- [ ] `get_db()` / `get_redis()` 依赖已实现
- [ ] `require_admin` 依赖：无 token 抛 40003 / token 无效抛 40003 / admin disabled 抛 40003
- [ ] `require_event_api_key` 依赖：从 `X-Event-Api-Key` Header 校验
- [ ] FastAPI `OAuth2PasswordBearer(tokenUrl="/admin/login")` 已配置

### B.3 响应与异常

- [ ] `core/responses.py` 已创建：`ok / fail / paged`
- [ ] `main.py` 已注册全局异常处理器：`BusinessException / RequestValidationError / HTTPException`
- [ ] 所有错误响应格式 `{code, message, data}`
- [ ] 验证错误统一返回 40101

### B.4 依赖与配置

- [ ] `requirements.txt` 补 `python-jose[cryptography]` / `passlib[bcrypt]` / `openpyxl`
- [ ] `config.py` 补 `event_api_key: str = ""` 字段
- [ ] `.env.example` 同步补 `EVENT_API_KEY` 示例
- [ ] `main.py` 已注册 `admin_router` 与 `events_router`

## C. 数据层变更（必须先行）

涉及文件：

- `backend/app/models.py`
- `backend/sql/schema.sql`
- `backend/sql/seed.sql`

### C.1 EventLog 表

- [ ] `models.py` 新增 `EventLog` 类
- [ ] 字段：`id / event_type / userid / target_type / target_id / occurred_at / extra / created_at`
- [ ] 索引 `idx_target(target_type, target_id, occurred_at)` 与 `idx_user_time(userid, occurred_at)`
- [ ] `schema.sql` 新增 CREATE TABLE 语句
- [ ] `seed.sql` 不需新增 event_log 数据

### C.2 audit_action 枚举扩展

- [ ] `models.py` 中 `AuditLog.action` 枚举新增 `manual_edit` / `undo`
- [ ] `schema.sql` 中 `ALTER TABLE audit_log MODIFY COLUMN action ENUM(...)` 语句已写
- [ ] 现有数据兼容（仅新增枚举值，不删除）

### C.3 system_config 新增

- [ ] `seed.sql` 新增 5 个 key：
  - `event.dedupe_window_seconds`
  - `audit.lock_ttl_seconds`
  - `audit.undo_window_seconds`
  - `report.cache_ttl_seconds`
  - `account.import_max_rows`

## D. 登录与鉴权（模块 B）

涉及文件：

- `backend/app/api/admin/auth.py`（新建）
- `backend/app/services/admin_user_service.py`（新建）
- `backend/app/schemas/admin.py`（修改：补 `ChangePasswordRequest`）

### D.1 登录

- [ ] `POST /admin/login` 已实现
- [ ] 用户名 / 密码错误返回 40001
- [ ] 失败计数：`admin_login_fail:{username}` Redis 计数
- [ ] 连续 ≥ 3 次失败后服务端 sleep 1 秒
- [ ] enabled=0 返回 40301
- [ ] 成功后 `last_login_at` 更新
- [ ] 成功后清除失败计数
- [ ] 返回 `{access_token, token_type, expires_at, password_changed}`

### D.2 当前用户

- [ ] `GET /admin/me` 已实现
- [ ] 返回 `AdminUserRead`（不含密码哈希）

### D.3 改密码

- [ ] `PUT /admin/me/password` 已实现
- [ ] 旧密码错误返回 40001
- [ ] 新密码长度 < 8 返回 40101
- [ ] 新密码与旧密码相同返回 40101
- [ ] 成功后 `password_changed=1`，旧 token 仍然有效

## E. 审核工作台（模块 C）

涉及文件：

- `backend/app/api/admin/audit.py`（新建）
- `backend/app/services/audit_workbench_service.py`（新建）
- `backend/app/schemas/audit.py`（新建）
- `backend/app/core/redis_client.py`（修改：补软锁 / Undo 工具）

### E.1 软锁与 Undo 工具

- [ ] `acquire_audit_lock / release_audit_lock / get_audit_lock_holder` 已实现
- [ ] 软锁 TTL 默认 300 秒，可从 `system_config` 读取
- [ ] `save_undo / pop_undo` 已实现
- [ ] Undo TTL 默认 30 秒

### E.2 队列与详情

- [ ] `GET /admin/audit/queue` 支持分页、status / target_type 筛选
- [ ] 队列项含 `locked_by`、`risk_level`、`extracted_brief`
- [ ] `GET /admin/audit/pending-count` 返回 `{job, resume, total}`
- [ ] `GET /admin/audit/{type}/{id}` 详情含：
  - `version / locked_by / risk_level / trigger_rules`
  - `submitter_history.{passed, rejected, last_7d}`
  - `extracted_fields / field_confidence`
  - `audit_status / created_at / owner_userid / raw_text`

### E.3 锁与释放

- [ ] `POST /lock`：被他人持有返回 40901，data 含 `locked_by`
- [ ] `POST /lock`：自己再次 lock 应当成功（续期或保持）
- [ ] `POST /unlock`：仅持有者可释放，否则不动作

### E.4 通过 / 驳回 / 编辑

- [ ] `POST /pass`：必须带 `version`，不一致返回 40902
- [ ] `POST /pass`：写 `audit_log` action=`manual_pass`，含 before/after snapshot
- [ ] `POST /pass`：写入 Undo Redis（30s）
- [ ] `POST /reject`：必须带 `reason`；可选 `notify` / `block_user`
- [ ] `POST /reject`：`block_user=true` 时同步封禁用户
- [ ] `PUT /edit`：仅允许字段白名单
- [ ] `PUT /edit`：字段值必须经 pydantic 校验
- [ ] 三类动作均触发 `version += 1`
- [ ] 三类动作均写 `audit_log`

### E.5 Undo

- [ ] `POST /undo`：从 Redis 取出快照，恢复字段
- [ ] 超过 30 秒返回 40903
- [ ] Undo 后该 Redis key 立即删除
- [ ] Undo 行为写 `audit_log` action=`undo`

## F. 账号管理（模块 D）

涉及文件：

- `backend/app/api/admin/accounts.py`（新建）
- `backend/app/services/account_service.py`（新建）
- `backend/app/schemas/account.py`（新建）

### F.1 厂家 / 中介

- [ ] `GET /admin/accounts/factories`：分页 / 筛选 / 搜索 keyword
- [ ] `POST /admin/accounts/factories`：预注册
- [ ] `external_userid` 后端可生成或前端指定
- [ ] external_userid 重复返回 40904
- [ ] `GET / PUT /admin/accounts/factories/{userid}`
- [ ] 中介路由结构与厂家一致，多 `can_search_jobs / can_search_workers` 字段
- [ ] 预注册 / 编辑 / 封禁 写 `audit_log`

### F.2 Excel 批量导入

- [ ] `POST /admin/accounts/factories/import` multipart/form-data
- [ ] 文件大小 ≤ 2MB
- [ ] 单批最大 `account.import_max_rows`
- [ ] 公式注入防护：`= + - @` 开头字段去除或转义
- [ ] 任意一行失败 → 全部回滚
- [ ] 返回 `{success_count, failed: [{row, error}]}`
- [ ] 列：`role, display_name, company, contact_person, phone, can_search_jobs, can_search_workers, external_userid?`

### F.3 工人列表

- [ ] `GET /admin/accounts/workers` 只读
- [ ] 不提供 POST / PUT / DELETE
- [ ] `GET /admin/accounts/workers/{userid}` 只读

### F.4 黑名单

- [ ] `GET /admin/accounts/blacklist` 列表
- [ ] `POST /admin/accounts/{userid}/block` 必填 `reason`
- [ ] 已封禁返回 40904
- [ ] 封禁后 `user.status=blocked`、`blocked_reason=...`
- [ ] 写 `audit_log` action=`manual_reject`、target_type=`user`
- [ ] `POST /admin/accounts/{userid}/unblock` 必填 `reason`
- [ ] 解封后 `user.status=active`、`blocked_reason=NULL`

## G. 岗位 / 简历管理（模块 E）

涉及文件：

- `backend/app/api/admin/jobs.py`（新建）
- `backend/app/api/admin/resumes.py`（新建）
- `backend/app/services/job_admin_service.py`（新建，可在已有 service 上补方法）
- `backend/app/services/resume_admin_service.py`（同上）
- `backend/app/schemas/job.py` / `resume.py`（修改：补 admin DTO）

### G.1 列表与详情

- [ ] 列表筛选白名单与 §5.4 一致
- [ ] 排序参数 `sort=field:asc|desc`，字段在白名单内
- [ ] 分页默认 size=20，max=100
- [ ] 详情返回完整字段（除内部字段）
- [ ] 列表项可见到 `audit_status / delist_reason / expires_at / version`

### G.2 编辑

- [ ] `PUT /admin/jobs/{id}` / `/admin/resumes/{id}` 必须带 `version`
- [ ] `version` 不一致返回 40902
- [ ] 编辑后 `version += 1`，写 `audit_log` action=`manual_edit`
- [ ] 编辑字段白名单与 §5.4 一致
- [ ] 不允许修改 `id / owner_userid / created_at / version`

### G.3 下架 / 延期 / 取消下架

- [ ] `POST /delist`：`{reason: "manual_delist"|"filled"}`
- [ ] 下架后 `delist_reason=...`，写 audit_log
- [ ] `POST /extend`：`{days: 15|30}`
- [ ] 延期后 `expires_at=max(now, expires_at)+days`，但不超过 `ttl.job.days*2`
- [ ] `POST /restore`：仅 `delist_reason IS NOT NULL` 且 `expires_at>now()` 时可用
- [ ] 取消下架后 `delist_reason=NULL`

### G.4 导出

- [ ] `GET /admin/jobs/export`、`/admin/resumes/export` 返回 CSV
- [ ] 文件名带时间戳：`jobs_{yyyyMMddHHmm}.csv`
- [ ] UTF-8 BOM 头
- [ ] 默认上限 10000 行，超出抛 40101
- [ ] 排除内部字段（如 `extra` 中的临时字段）

## H. 字典管理（模块 F）

涉及文件：

- `backend/app/api/admin/dicts.py`（新建）
- `backend/app/services/dict_service.py`（新建）
- `backend/app/schemas/dict.py`（新建）

### H.1 城市

- [ ] `GET /admin/dicts/cities` 支持按省份分组返回
- [ ] `PUT /admin/dicts/cities/{id}` 仅允许编辑 `aliases`
- [ ] 编辑后写 audit_log

### H.2 工种

- [ ] `GET / POST / PUT / DELETE /admin/dicts/job-categories[/{id}]`
- [ ] `code / name` 重复返回 40904
- [ ] 删除前检查是否被引用，引用中返回 40904
- [ ] 排序通过 `sort_order` 字段

### H.3 敏感词

- [ ] `GET / POST / DELETE /admin/dicts/sensitive-words[/{id}]`
- [ ] `word` 重复返回 40904
- [ ] `POST /admin/dicts/sensitive-words/batch`：返回 `{added, duplicated}`
- [ ] 字典变更通知 `audit_service.invalidate_cache()`

## I. 系统配置（模块 G）

涉及文件：

- `backend/app/api/admin/config.py`（新建）
- `backend/app/services/system_config_service.py`（新建）

### I.1 列表

- [ ] `GET /admin/config` 按 key 第一段分组
- [ ] 每项含 `config_key / config_value / value_type / description / updated_at / updated_by`
- [ ] 不暴露 `app_secret_key / wecom_aes_key / admin_jwt_secret` 等 .env 字段

### I.2 单项更新

- [ ] `PUT /admin/config/{key}` 已实现
- [ ] 配置不存在返回 40401
- [ ] 类型校验：`int/bool/json/string`
- [ ] 危险项变更（`filter.* / llm.provider`）写 audit_log
- [ ] 更新后清除 Redis `config_cache:{key}` 与 `config_cache:all`
- [ ] 返回 `{changed: bool, danger: bool}`

### I.3 与 Phase 4 联动

- [ ] 修改 `rate_limit.window_seconds / rate_limit.max_count` 后，webhook 限流行为立即变化（端到端可验证）

## J. 数据看板（模块 H）

涉及文件：

- `backend/app/api/admin/reports.py`（新建）
- `backend/app/services/report_service.py`（新建）
- `backend/app/schemas/report.py`（新建）

### J.1 接口

- [ ] `GET /admin/reports/dashboard` 含 today / yesterday / trend_7d
- [ ] `GET /admin/reports/trends?range=7d|30d|custom&from=&to=`
- [ ] `GET /admin/reports/top?dim=city|job_category|role&limit=10`
- [ ] `GET /admin/reports/funnel`
- [ ] `GET /admin/reports/export?metric=&from=&to=&format=csv`

### J.2 实现

- [ ] Dashboard 数据缓存 60 秒（key=`report_cache:dashboard`）
- [ ] 单接口耗时（缓存外）< 500ms
- [ ] DAU 按角色拆分：worker / factory / broker
- [ ] 命中率 / 空召回率从 conversation_log 聚合
- [ ] 详情点击率从 event_log 聚合
- [ ] 漏斗阶段固定：注册 → 首次发消息 → 首次有效检索 → 收到推荐 → 点详情

## K. 对话日志（模块 I）

涉及文件：

- `backend/app/api/admin/logs.py`（新建）
- `backend/app/services/log_service.py`（新建）
- `backend/app/schemas/conversation.py`（修改：补 admin DTO）

- [ ] `GET /admin/logs/conversations` 必传 `userid + start + end`
- [ ] `(end - start).days > 30` 抛 40101
- [ ] 支持 `direction / intent` 筛选
- [ ] 默认按时间倒序，page/size 默认 50/200
- [ ] `GET /admin/logs/conversations/export` 返回 CSV
- [ ] 不实现全文检索

## L. 事件回传 API（模块 J）

涉及文件：

- `backend/app/api/events.py`（新建）
- `backend/app/services/event_service.py`（新建）
- `backend/app/schemas/event.py`（新建）

- [ ] `POST /api/events/miniprogram_click` 已实现
- [ ] 鉴权 `X-Event-Api-Key`：错误返回 40001
- [ ] 请求体：`{userid, target_type, target_id, timestamp?}`
- [ ] 幂等：`event_idem:{userid}:{target_type}:{target_id}` SETNX EX 600
- [ ] 已去重时仍返回 `code=0`，`data.deduped=true`
- [ ] 写入 `event_log` 表
- [ ] 写入失败时写 audit_log 并降级，**不阻塞业务回包**
- [ ] 路由不在 `/admin/*` 下，独立 `/api/events`

## M. 全局错误码与文档

- [ ] 错误码与 §6.4 一致
- [ ] FastAPI Swagger UI 可见所有路由
- [ ] 每个路由含 `summary` 与 `description`
- [ ] 每个 schema 含字段注释（pydantic `Field(description=...)`）

## N. 自动化测试

- [ ] 登录 / 改密 / token 鉴权 单测
- [ ] 审核：lock 冲突、版本冲突、Undo 30s 边界 单测
- [ ] 审核：pass / reject / edit / undo 写 audit_log 单测
- [ ] 账号：预注册重复 / Excel 导入成功+失败 单测
- [ ] 黑名单：封禁/解封 单测
- [ ] 岗位：列表筛选、延期上限、取消下架边界 单测
- [ ] 字典：唯一性 / 引用检查 / 批量去重 单测
- [ ] 配置：危险项写 audit_log + 缓存清理 单测
- [ ] 报表：Dashboard 缓存命中 / 60 秒过期 单测
- [ ] 日志：30 天范围限制 单测
- [ ] 事件回传：API Key / 幂等 / 写入失败降级 单测
- [ ] 端到端：修改限流配置后 Phase 4 webhook 行为变化（集成测试，可标 marker）

## O. 收尾确认

- [ ] 全部 50 个端点可在 Swagger UI 调通
- [ ] 无 `/admin/*` 接口可匿名访问
- [ ] 无接口直接返回 ORM 对象
- [ ] 无写操作绕过 audit_log
- [ ] 无 admin 接口直接拼裸 SQL
- [ ] requirements.txt 已补依赖
- [ ] schema.sql / seed.sql / models.py 三者一致
- [ ] `main.py` 已注册 `admin_router` 与 `events_router`
- [ ] 全局异常处理器已生效
