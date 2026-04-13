# Phase 3 开发实施文档

> 基于：`collaboration/features/phase3-main.md`
> 面向角色：后端开发
> 状态：`draft`
> 创建日期：2026-04-13

## 1. 开发目标

本阶段开发目标，是把 JobBridge 的“真正业务脑子”做出来，而不是继续补基础设施。

开发时请始终记住：

- Phase 2 已经把 provider、storage、wecom、Redis 基础能力封好了，Phase 3 不要重新发明一层
- Phase 3 交付的是可独立测试的业务 service，不是 webhook、worker 或 admin API
- 本阶段必须把 prompt 变成业务可用版，但“最终用户回复文案”仍由 service 层掌控，不能把业务逻辑写回 provider
- 权限过滤和最终回复拼装必须放在 service 层，不能寄希望于前端或小程序兜底

## 2. 当前代码现状

当前仓库已具备：

- `app/models.py`、`app/schemas/*` 已完成
- `app/llm/*` 已有 provider 工厂与基础 fallback
- `app/storage/*`、`app/wecom/*`、`app/core/redis_client.py` 已可复用
- `docs/message-contract.md` 已锁定消息基础契约

当前缺失：

- `app/services/` 下所有业务 service
- 业务版 prompt
- Phase 3 对应自动化测试
- 可独立运行的 service smoke 流程

特别注意：

- `app/services/` 目前是空目录，Phase 3 的文件职责需要一次性定清
- `llm/prompts.py` 目前还是骨架版，Phase 3 必须升级为业务版
- `app/services/__init__.py` 在 Phase 3 不强制做 re-export，调用方直接 import 子模块；若后续统一导出，再在 Phase 4 收口

## 3. 开发原则

### 3.1 依赖边界

- service 只依赖 `models`、`schemas`、`app.llm` 工厂、`app.storage` 工厂、`app.core.redis_client`
- service 不允许直接 import `app.llm.providers.*`
- service 不允许把业务判断写进 `wecom/`、`storage/`、`llm/providers/`
- 本阶段不引入 `message_router.py` 和 `webhook.py`

### 3.2 代码内字段命名统一

- Redis session 中的 `search_criteria`、`criteria_patch.field`、`structured_data` 一律使用英文 canonical key
- 上传实体字段直接对齐 ORM 字段
- 检索条件中 `city`、`job_category` 一律按 `list[str]` 存储
- 中文字段名只存在于 prompt 示例和最终回复文案，不进入持久化状态

### 3.3 安全优先于生成式文案

- 工人侧最终回复文本不能直接透传 `Reranker.reply_text`
- 最终对外文本必须基于 `permission_service` 过滤后的结构化字段重新拼装
- 审核未通过的条目、已下架岗位、过期条目都不能进入召回池

### 3.4 不越界

本阶段不要顺手做：

- webhook 入站逻辑
- worker 消费逻辑
- admin API
- 前端联调
- 向量检索
- OCR 或语音识别

## 4. 建议开发顺序

### 第 1 步：先锁定 prompt v2.0 和 `intent_service`

优先完成：

- `app/services/intent_service.py`
- `app/llm/prompts.py`

原因：

- Phase 3 几乎所有 service 都依赖 canonical key、intent 结果和 missing fields 口径
- 如果 prompt 和结构化 key 不先锁住，后面的 upload/search/conversation 都会反复返工

本步需同时明确：

- 命令归并规则
- `show_more` 同义语判定
- `structured_data` key 形状
- `criteria_patch` 语义
- `missing_fields` 口径

### 第 2 步：实现 `conversation_service`

在开始 upload/search 前，先把 session 契约做稳定：

- 读写 SessionState
- merge patch
- 生成 query_digest
- 管理快照
- `/重新找`
- `broker_direction`
- `follow_up_rounds`

理由：

- Phase 3 的 follow_up、show_more、broker sticky 都依赖 conversation 基础能力

### 第 3 步：实现 `user_service`

完成：

- 自动注册
- 角色识别
- 首次欢迎
- blocked / deleted 判定
- `last_active_at`
- `/我的状态`

理由：

- 后续上传和检索流程都需要一个稳定的用户上下文对象

### 第 4 步：实现 `audit_service` 与 `permission_service`

原因：

- 上传链路必须先有审核口径
- 检索链路必须先有权限过滤口径
- 这两块都是“后补最危险”的模块，应该早做并早测

### 第 5 步：实现 `upload_service`

优先把“上传 → 追问 → 审核 → 入库”跑通：

- 岗位上传
- 简历上传
- 图片 key 留存
- 删除流程依赖的数据结构收口

### 第 6 步：实现 `search_service`

最后做：

- 硬过滤
- 候选集裁剪
- rerank
- 宽松匹配
- 快照翻页格式化

原因：

- 它依赖前面所有 service 的口径都稳定

### 第 7 步：补 smoke 与自动化测试

不要等全部写完再开始测。

建议：

- 每完成一个 service，就补对应单测
- `upload_service` 和 `search_service` 完成后，再补集成测试
- 最后补一个可运行的 Phase 3 smoke 流程

## 5. 文件级实施要求

### 5.1 `backend/app/services/intent_service.py`

必须包含：

- 显式命令识别
- `show_more` 同义语识别
- 对 `IntentExtractor` 的统一封装
- `missing_fields` 规范化
- `structured_data` / `criteria_patch` 的 canonical key 校验

实现要求：

- 显式命令优先于普通自然语言
- 命令统一归并到固定命令集，不保留大量别名分叉
- LLM 结果进入业务层前先做一次 shape 校验和类型整理
- `criteria_patch` 若出现未知字段，必须丢弃并记录日志，不得直接写入 session
- provider fallback 行为由 Phase 2 保底，本 service 只负责把 fallback 结果转换成业务可用分支

### 5.2 `backend/app/llm/prompts.py`

必须更新：

- Intent prompt 升级为业务版
- Rerank prompt 升级为业务版
- 版本号至少升级到 `v2.0`

必须明确写入 prompt 的内容：

- worker / factory / broker 的角色上下文
- 严格 JSON 输出
- 不允许 markdown code block
- canonical key 命名要求
- `criteria_patch` 语义
- 必填字段范围
- 不主动追问民族、纹身、健康证、禁忌
- 边界输入 fallback

建议 few-shot 组合：

- 工人找岗位
- 厂家发布岗位或发布后顺便找工人
- follow_up / patch 修正
- 边界输入（闲聊 / 表情 / 过短文本）

### 5.3 `backend/app/services/conversation_service.py`

必须包含：

- `load_session()`
- `save_session()`
- `merge_criteria_patch()`
- `reset_search()`
- `clear_session()`
- `record_history()`
- `record_shown()`
- `build_snapshot()`
- `save_snapshot()`
- `get_next_candidate_ids()`

关键实现要求：

- `search_criteria` 变更后必须清空快照和 `shown_items`
- `query_digest` 基于排序后的 canonical key JSON 计算
- history 最多保留 12 条 message
- `shown_items` 去重
- show_more 从快照剩余 ID 中顺序取下一批，不重新检索
- `broker_direction` 固定取值为 `search_job` / `search_worker`
- `follow_up_rounds` 固定存于 session，用于约束“最多连续追问 2 轮”
- `conversation_service` 负责快照的存取与失效管理，不负责决定何时生成候选 ID 列表
- 快照的生成时机由 `search_service` 决定；`search_service` 在拿到 rerank 结果后调用 `conversation_service.save_snapshot()`

### 5.4 `backend/app/services/user_service.py`

必须包含：

- 用户识别 / 自动注册
- 首次欢迎判定
- blocked / deleted 拦截
- 活跃时间更新
- `/我的状态` 查询
- `/删除我的信息` 的用户状态更新入口

关键实现要求：

- 未预注册用户一律默认建成 `worker`
- `deleted` 不自动恢复
- `deleted` 用户人工恢复路径留给 Phase 5 admin API；Phase 3 仅做拦截+提示
- 首次欢迎以 `last_active_at IS NULL` 判定
- 返回给调用方的用户上下文必须带上：
  - role
  - status
  - company
  - contact_person
  - phone
  - can_search_jobs
  - can_search_workers
  - is_first_touch

### 5.5 `backend/app/services/audit_service.py`

必须包含：

- 敏感词扫描
- 风险等级聚合
- LLM 安全检查接入点
- 审核结果对象
- `audit_log` 写入 helper

关键实现要求：

- `high -> rejected`
- `mid -> pending`
- `low -> passed with tag`
- 外部安全接口失败时有受控退化
- `pending` 不强行写 `audit_log.action`
- `passed` / `rejected` 写 `audit_log`

### 5.6 `backend/app/services/permission_service.py`

必须包含：

- 岗位结果过滤方法
- 简历结果过滤方法
- 角色路由判断

关键实现要求：

- 工人侧必须去掉电话、详细地址、歧视性展示字段
- 厂家/中介侧可看电话，但电话缺失时要返回固定占位文案
- 过滤后返回结构化数据，而不是只返回最终字符串

### 5.7 `backend/app/services/upload_service.py`

必须包含：

- 岗位上传处理
- 简历上传处理
- 缺失字段追问生成
- 审核接入
- TTL 和审计字段写入

关键实现要求：

- 消费 `IntentResult`，不重复调 LLM
- 图片只写 key
- `expires_at` 读 `system_config`
- 对 `pending` / `rejected` / `passed` 产出不同结果分支
- 追问最多连续 2 轮，轮数应能被 session 或调用方显式追踪

### 5.8 `backend/app/services/search_service.py`

必须包含：

- `search_jobs()`
- `search_workers()`
- 中介双向模式不强制单独新增 `search_by_broker()`；Phase 3 由调用方依据 session 内的 `broker_direction` 选择调用 `search_jobs()` 或 `search_workers()`
- `format_first_batch()`
- `format_next_batch()`

关键实现要求：

- base query 必须过滤 `audit_status/deleted_at/expires_at/delist_reason/user.status`
- 先裁剪候选集到 50 条以内，再调 Reranker
- 0 召回时不调 Reranker
- 候选不足时只做一次薪资放宽 10% 的宽松匹配
- “只做一次”定义为单次 `search_service` 调用内的重试，不跨 session 记忆
- `show_more` 基于快照 ID 重新查实体并过滤失效项
- 最终文本基于过滤后的结构化字段拼装

### 5.9 测试与运行说明

建议新增测试文件：

- `backend/tests/unit/test_intent_service.py`
- `backend/tests/unit/test_conversation_service.py`
- `backend/tests/unit/test_user_service.py`
- `backend/tests/unit/test_audit_service.py`
- `backend/tests/unit/test_permission_service.py`
- `backend/tests/unit/test_upload_service.py`
- `backend/tests/unit/test_search_service.py`
- `backend/tests/integration/test_phase3_upload_and_search.py`
- `backend/tests/integration/test_phase3_delete_flow.py`
- `backend/tests/integration/test_phase3_broker_flow.py`
- `backend/tests/integration/test_phase3_upload_then_search_smoke.py`

同时更新：

- `backend/tests/README.md`

至少补一条 smoke 入口，形式可以是：

- 集成测试文件
- 或单独脚本，但必须写入 README

## 6. 推荐测试策略

开发自测建议分三层推进：

1. 纯单元测试
- prompt 升级
- intent 归并
- patch merge
- permission 过滤
- audit 判定

2. 集成测试
- 上传链路
- 检索链路
- 删除链路
- show_more

3. smoke 流程
- 工人找岗位
- 厂家找工人
- 中介切换方向后检索

## 7. 自测通过标准

开发提交前，至少确认：

- `app/services/` 下 Phase 3 所有文件已创建
- prompt 已升级为业务版，版本号已更新
- `search_criteria` / `criteria_patch` 一律使用 canonical key
- `show_more` 不会重新触发 rerank
- 工人侧最终回复不泄漏电话、详细地址、歧视性字段
- 上传待审/驳回条目不会进入召回池
- `/删除我的信息` 会清空 session 并软删除数据
- 自动化测试覆盖核心 service
- README 或等价运行说明已更新
- 本阶段代码没有越界到 Phase 4 的 webhook / worker / router

## 8. 交付要求

开发提交时应一并提供：

- 代码
- 自动化测试
- README / 运行说明更新
- 如新增配置项，同步 `.env.example` 或 `seed.sql`
- 如新增 `system_config` 依赖项，必须同步更新 `backend/sql/seed.sql`
- 已知限制列表

## 9. 已知风险提醒

- 如果 `Reranker.reply_text` 被直接透传，权限泄漏风险极高
- 如果 `criteria_patch` 不锁定英文 key，后续 session 和测试会持续失配
- 如果删除流程只改 `user.status` 不清 session / 简历 / 对话日志，Phase 3 视为未完成
