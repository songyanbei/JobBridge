# Feature: Phase 3 核心业务服务与 Prompt 定稿
> 状态：`draft`
> 创建日期：2026-04-13
> 对应实施阶段：Phase 3
> 关联实施文档：`docs/implementation-plan.md` §4.4
> 关联方案设计章节：§2.3、§6、§7、§8、§9、§10、§11.8、§12.1、§17.2
> 关联架构章节：§三、§4.1、§4.4、§4.5、§五
> 配套文档：
> - 开发实施文档：`collaboration/features/phase3-dev-implementation.md`
> - 开发 Checklist：`collaboration/features/phase3-dev-checklist.md`
> - 测试实施文档：`collaboration/features/phase3-test-implementation.md`
> - 测试 Checklist：`collaboration/features/phase3-test-checklist.md`

## 1. 阶段目标

Phase 3 的目标，是在不依赖企微 webhook、worker、admin API 和前端页面的前提下，把 JobBridge MVP 的核心业务能力真正跑通。

本阶段完成后，项目至少应具备以下能力：

- service 层已形成稳定的业务编排骨架，后续 Phase 4 只需接入消息路由和企微上下行链路
- prompt 已从 Phase 2 的骨架版升级为业务可用版，能够支撑意图识别、结构化抽取、追问和重排
- 工人、厂家、中介三类典型业务流程可通过脚本或集成测试独立跑通
- 会话状态、快照分页、字段级权限过滤、审核前置、删除流程都已有明确实现口径
- 测试同学不依赖真实企微环境，即可完成本阶段功能验证和回归

## 2. 当前代码现状

当前仓库内与 Phase 3 直接相关的基础已经具备：

- `backend/app/models.py`、`backend/app/schemas/*` 已完成 Phase 1 数据层交付
- `backend/app/llm/*`、`backend/app/storage/*`、`backend/app/wecom/*` 已完成 Phase 2 基础设施交付
- `backend/app/core/redis_client.py` 已具备 session、分布式锁、限流、幂等、队列等基础能力
- `docs/message-contract.md` 已锁定企微消息基础契约，供 Phase 4 直接复用
- `backend/tests/unit/` 与 `backend/tests/integration/` 已具备基础设施层自动化测试

当前仍然缺失的部分：

- `backend/app/services/` 目录目前只有 `__init__.py`，尚无任何业务 service
- `backend/app/llm/prompts.py` 仍是 v1.0 骨架版，few-shot、字段映射、角色差异化表达和追问策略尚未定稿
- 目前没有面向上传、检索、审核、权限过滤、删除流程的自动化测试
- 当前没有可用于 Phase 3 的 smoke 脚本或端到端服务流集成测试

Phase 2 handoff 中已明确的起点：

- `llm/prompts.py` 已有 `INTENT_*` / `RERANK_*` 常量，但仍需升级到业务版 prompt
- provider 层的 JSON fallback、超时重试、`raw_response` 保留策略已锁定
- 统一消息对象、对象存储、本地 Redis/DB 基础能力均已就绪，不应在 Phase 3 重复设计

## 3. 本阶段范围

### 3.1 本阶段必须完成

1. `user_service.py`
- 用户识别
- 工人自动注册
- 厂家/中介首次欢迎判断
- 封禁/删除状态处理
- `last_active_at` 更新

2. `intent_service.py` 与 prompt 定稿
- 命令优先识别
- 意图分类
- 结构化抽取
- 必填字段缺失识别
- prompt 从骨架版升级为业务版（建议版本号升级为 `v2.0`）

3. `conversation_service.py`
- Redis session 读写
- `criteria_patch` merge
- `candidate_snapshot` 生命周期管理
- `shown_items` 记录与去重
- `/重新找` 清空
- 中介方向 sticky 所需状态承载

4. `audit_service.py`
- 敏感词检测
- LLM 安全检查接入点
- 自动通过 / 自动拒绝 / 进入人工审核队列的判定
- 审核日志写入

5. `permission_service.py`
- 岗位结果对工人视角的字段脱敏
- 简历结果对厂家/中介视角的字段过滤
- 工人侧歧视性字段展示脱敏

6. `upload_service.py`
- 岗位/简历入库编排
- 必填字段缺失追问编排
- 图片仅留存不抽取
- 审核接入

7. `search_service.py`
- 硬过滤
- 候选集构造
- Reranker 调用
- 权限过滤
- 最终回复格式化
- show_more 所需的快照分页支持

8. 命令相关的 service 基座
- `/重新找` 对应的 session reset
- `/删除我的信息` 对应的数据清理与状态更新
- `/找岗位`、`/找工人` 的中介方向切换基础能力
- `/我的状态` 所需的账号状态聚合查询

9. 自动化测试与可运行验证
- 单元测试覆盖核心 service 逻辑
- 集成测试覆盖三类典型流程
- 至少一条可复现的 Phase 3 smoke 流程

### 3.2 本阶段明确不做

- 不实现 `api/webhook.py`
- 不实现 `services/worker.py`
- 不实现 `services/message_router.py`
- 不实现 admin API
- 不实现前端页面或前后端联调
- 不引入向量检索、RAG、知识库
- 不让图片参与一期结构化抽取
- 不把 Redis session 改写为 MySQL 存储
- 不实现自动封禁规则
- 不实现岗位 headcount 自动递减
- 不要求真实企微链路、真实豆包安全接口联调作为通过前提

特别说明：

- `/续期`、`/下架`、`/招满了` 的消息路由和企微链路接入留给 Phase 4
- 如本阶段需要为这些命令预留底层数据变更方法，允许在不引入 `message_router.py` 的前提下增加 service 内部 helper

## 4. 真值来源与实现基线

出现冲突时，按以下优先级执行：

1. `docs/implementation-plan.md` §4.4
2. `方案设计_v0.1.md` §2.3、§6、§7、§8、§9、§10、§11.8、§17.2
3. `docs/architecture.md` §三、§4.1、§4.4、§4.5、§五
4. `docs/message-contract.md`
5. `collaboration/features/phase2-main.md` §10 handoff
6. 本文档

本阶段额外锁定以下代码级约束，避免实现歧义：

- `search_criteria`、`criteria_patch.field`、`structured_data` 在代码和 Redis 中一律使用英文 canonical key，不使用中文展示字段名
- 上传场景的结构化字段名直接对齐 ORM 字段，如 `city`、`job_category`、`salary_floor_monthly`
- 检索场景的 `search_criteria` 中，`city`、`job_category` 统一按 `list[str]` 存储，即便只有 1 个值也存列表
- `Reranker.reply_text` 不可直接作为工人侧最终回复透传，最终发送文本必须基于权限过滤后的结构化字段再组装
- `criteria_patch` 的语义固定为：
  - `add`：仅用于列表型字段，做去重追加
  - `update`：替换标量字段或整列表
  - `remove`：若给定值则从列表字段移除该值；若值为 `null` 则删除整个字段
- 方案设计中关于 `search_criteria` 的中文示例仅表示语义，不作为代码字段命名依据

### 4.1 Phase 3 依赖的 `system_config` key

Phase 3 文档、开发和测试统一按以下现有 key 读取配置；如新增依赖项，必须同步更新 `backend/sql/seed.sql`：

| key | 用途 | 默认值来源 |
|---|---|---|
| `match.top_n` | 首轮推荐条数 | `backend/sql/seed.sql` |
| `ttl.job.days` | 岗位 TTL 天数 | `backend/sql/seed.sql` |
| `ttl.resume.days` | 简历 TTL 天数 | `backend/sql/seed.sql` |
| `filter.enable_gender` | 是否启用性别过滤 | `backend/sql/seed.sql` |
| `filter.enable_age` | 是否启用年龄过滤 | `backend/sql/seed.sql` |
| `filter.enable_ethnicity` | 是否启用民族过滤 | `backend/sql/seed.sql` |

特别说明：

- “宽松匹配”不依赖 `system_config` 开关，Phase 3 固定只在单次搜索请求内执行一次薪资放宽 10%
- 如 Phase 3 需要把“最大追问轮数”等参数配置化，必须新增对应 `system_config` key 并同步补充 `seed.sql`

## 5. 详细需求说明

### 5.1 `user_service.py`

涉及文件：

- `backend/app/services/user_service.py`

要求：

- 输入至少包括 `external_userid`，可选带入企微联系人快照
- 输出必须能让调用方明确知道：
  - 用户是否已存在
  - 角色是什么
  - 是否是首次交互
  - 是否应发送欢迎语
  - 是否已封禁 / 已删除
  - 当前是否允许继续进入业务处理
- 未预注册用户默认按 `worker` 自动注册
- 未预注册用户即使发“我要招人”这类文本，也不能自动升格为厂家/中介，仍按工人注册并引导联系客服开企业账号
- 厂家/中介首轮欢迎判定以 `last_active_at IS NULL` 为准
- 工人只在首次自动注册时发送欢迎语，session 过期后再次发消息不重复欢迎
- `status=blocked` 用户必须被短路，返回封禁提示，不继续进入 intent / upload / search 逻辑
- `status=deleted` 用户默认不自动恢复，返回“账号已进入删除状态，请联系客服处理”的受控提示，避免静默恢复已删除账号
- `deleted` 用户的人工恢复路径留给 Phase 5 admin API；Phase 3 只做拦截 + 提示，不在 service 层实现恢复
- 所有进入正常处理链路的消息都应更新 `last_active_at`
- `/我的状态` 所需的账号状态聚合查询能力由本 service 提供

### 5.2 `intent_service.py` 与 prompt 定稿

涉及文件：

- `backend/app/services/intent_service.py`
- `backend/app/llm/prompts.py`

要求：

- Phase 3 必须把 prompt 从骨架版升级为业务版，建议版本号升级为 `v2.0`
- Intent 识别顺序固定为：
  1. 显式命令匹配
  2. show_more 同义语匹配
  3. 再调用 `IntentExtractor`
- 命令识别必须归并到 §17.2 的固定命令集，不能在 service 层保留大量自由别名分支
- `IntentResult.structured_data` 的字段名必须对齐代码级 canonical key
- `missing_fields` 只允许返回 §10.4 定义的必填字段，不允许把民族、纹身、健康证、禁忌等敏感软字段塞进必填追问
- prompt 必须显式覆盖三类角色差异：worker / factory / broker
- prompt 至少覆盖以下 few-shot 场景：
  - 工人找岗位
  - 厂家发布岗位或发布并顺便找工人
  - 多轮追问 / criteria patch 修正
  - 边界输入（闲聊、表情、空内容、过短文本）
- prompt 中必须写清：
  - 严格 JSON 输出
  - 不允许 markdown code block
  - canonical key 命名
  - `criteria_patch` 语义
  - 缺失字段策略
  - 边界输入 fallback 策略
- `conversation_log.criteria_snapshot` 中必须记录本轮使用的 prompt 版本号，至少包含：
  - `intent_prompt_version`
  - `rerank_prompt_version`
- provider 层已经锁定的兜底规则继续沿用，Phase 3 不得反向改写 provider 行为
- “没太理解您的意思”“系统繁忙”等最终用户中文回复由 Phase 3 service 层决定，不得回写到 provider 层

### 5.3 `conversation_service.py`

涉及文件：

- `backend/app/services/conversation_service.py`

要求：

- 统一通过 `app.core.redis_client` 读写 session
- SessionState 必须符合 `schemas/conversation.py` 契约
- `history` 按“最近 6 轮对话”截断，代码实现统一为最多保留 12 条 message（user/assistant 交替）
- `search_criteria` 使用 canonical key，并做稳定排序后计算 `query_digest`
- `criteria_patch` merge 规则固定：
  - `add`：对列表做去重合并
  - `update`：整体替换
  - `remove`：删除列表项或删除整字段
- 每次 `search_criteria` 发生有效变化时，必须：
  - 更新 `updated_at`
  - 重新计算 `query_digest`
  - 清空旧 `candidate_snapshot`
  - 清空 `shown_items`
- 中介双向 sticky 的 session 字段固定为 `broker_direction`，取值固定为 `search_job` / `search_worker`
- 上传追问轮数的 session 字段固定为 `follow_up_rounds`，用于约束“最多连续追问 2 轮”
- `show_more` 必须复用 `candidate_snapshot`，不能重新执行一轮完整检索来代替翻页
- `record_shown()` 必须去重，并保留已展示顺序
- `/重新找` 执行后必须清空：
  - `search_criteria`
  - `candidate_snapshot`
  - `shown_items`
  - `follow_up_rounds`
  - 与当前检索相关的 `history` 可清空或仅保留系统确认消息，但同一实现必须固定
- `/重新找` 不清空 `broker_direction`；对中介而言，这代表“保留当前找岗位/找工人方向，只重置本轮检索条件”
- 中介双向 sticky 的状态承载放在 session 内，不新增额外存储

### 5.4 `audit_service.py`

涉及文件：

- `backend/app/services/audit_service.py`

要求：

- 审核判定对象为岗位和简历两类上传内容
- 敏感词字典只读取 `dict_sensitive_word.enabled=1` 的词
- 风险等级口径固定为：
  - `high`：自动拒绝
  - `mid`：进入人工审核队列（`audit_status=pending`）
  - `low`：允许通过，但保留命中标记写入 `extra` 或审计上下文
- 若接入 LLM 安全检查：
  - 只能提升风险等级，不能把高危词命中降级
  - 若外部安全接口不可用，必须有 no-op 或关闭开关的受控退化路径
- 审核结果至少统一为：
  - `passed`
  - `pending`
  - `rejected`
- `passed` / `rejected` 时写 `audit_log`
- `pending` 时由于 `audit_log.action` 无 pending 枚举，本阶段不强行写 audit_log，但必须把机器审核理由写入实体 `audit_reason`
- 所有未通过审核的条目都不得进入召回池
- 拒绝和待审都必须保留原始内容，供后续人工审核和申诉使用

### 5.5 `permission_service.py`

涉及文件：

- `backend/app/services/permission_service.py`

要求：

- 字段过滤规则以 §6.2 和 §7.5、§7.7 为准
- 对工人看岗位：
  - 必须隐藏电话
  - 必须隐藏详细地址 / 门牌
  - 必须隐藏歧视性展示字段，如 `gender_required`、`age_min/max`、`accept_minority`
- 对厂家/中介看简历：
  - 可返回姓名、年龄、性别、电话
  - 电话缺失时仍可返回候选人，但回复中必须用固定文案标明“联系方式待补充”
- 过滤发生在 service 层，不能依赖前端或小程序侧再脱敏
- 返回给调用方的结果应尽量保持结构化，便于 `search_service` 做最终文本拼装
- 不允许把未经权限过滤的实体对象直接暴露给最终回复拼装器

### 5.6 `upload_service.py`

涉及文件：

- `backend/app/services/upload_service.py`

要求：

- `upload_service` 只负责“上传编排”，不负责识别企微消息类型
- 输入至少包括：
  - 用户上下文
  - 原始文本
  - `IntentResult`
  - 已保存的图片 key 列表（可为空）
- `upload_service` 不重复调用 LLM 抽取；Phase 4 router 应先调 `intent_service`
- 图片只保存 key，不参与字段抽取
- 缺失必填字段时，由 `upload_service` 按 §10.4 规则返回追问文本：
  - 缺 1-2 个字段：合并成一句追问
  - 缺 3 个及以上：列表式引导
  - 最多连续追问 2 轮
- 追问轮数由 session 中的 `follow_up_rounds` 统一承载，不由未来 router 临时维护
- 上传场景在连续 2 轮追问后仍缺必填字段时，第 3 次不再继续追问，也不做“按残缺条件入库”；统一返回明确降级提示，要求用户补齐后重新提交
- 岗位 / 简历入库时必须写：
  - `raw_text`
  - `description`
  - `images`
  - `expires_at`
  - `audit_status`
  - `audit_reason`
  - `audited_by`
  - `audited_at`
- `expires_at` 从 `system_config` 读取 TTL 天数
- 上传成功但 `audit_status=pending/rejected` 时，也算“入库成功”，但回复文案应区分：
  - `passed`：已入库，可进入匹配池
  - `pending`：已收到，待人工审核
  - `rejected`：已收到，但未通过审核
- `upload_and_search` 的“两步编排”由 Phase 4 router 负责，本 service 只需保证上传能力可复用

### 5.7 `search_service.py`

涉及文件：

- `backend/app/services/search_service.py`

要求：

- `search_service` 至少支持：
  - 工人找岗位
  - 厂家找工人
  - 中介双向检索
  - 基于快照的 show_more 格式化
- 硬过滤必须同时包含业务条件和系统状态条件：
  - `audit_status='passed'`
  - `deleted_at IS NULL`
  - `expires_at > now`
  - 岗位额外要求 `delist_reason IS NULL`
  - 用户状态为 `active`
- 检索时必须补充关联用户信息：
  - 岗位结果需要 `user.company` / 联系人等展示数据
  - 简历结果需要 `user.display_name` / `user.phone`
- 进入 rerank 前必须限制候选集规模，默认上限 50 条；超过上限时按 `created_at DESC, id DESC` 截断
- 0 召回时不能调用 Reranker，应直接返回引导性空结果回复
- 候选数 `0 < count < top_n` 时，允许做一次“宽松匹配”重试
- 宽松匹配在 Phase 3 固定只做一件事：
  - 将薪资下限放宽 10%
- “宽松匹配只做一次”定义为单次 `search_service` 调用内的重试逻辑，不跨 session、不跨 follow_up 轮次记忆
- “扩邻近城市”因当前没有权威邻近城市映射，本阶段不自动猜测；如未来补齐映射配置，再按配置化方式扩展
- `match.top_n` 从 `system_config` 读取，默认值为 3
- `show_more` 不重新 rerank，只对快照剩余条目做：
  - 有效性过滤
  - 权限过滤
  - 最终文本格式化
- 若快照中部分条目已失效，需跳过并继续向后取，直到拿满 3 条或候选耗尽
- 最终发送文本必须基于权限过滤后的结构化字段组装，不得直接透传 `Reranker.reply_text`
- 工人侧回复格式需符合 §10.5，且不得泄漏电话、详细地址和歧视性字段

### 5.8 合规与命令基础能力

要求：

- Phase 3 不强制新增 `command_service.py`
- 命令的 service 层入口归属固定为：
  - `/重新找` -> `conversation_service`
  - `/找岗位`、`/找工人` -> `conversation_service`
  - `/我的状态` -> `user_service`
  - `/删除我的信息` -> `user_service`
- Phase 4 `message_router` 只负责命中命令后把请求路由到上述 service，不再二次定义命令归属
- `/删除我的信息` 必须可在 service 层独立执行，不依赖 webhook / worker / admin API
- 删除流程固定为：
  - 立即清空 Redis session
  - 立即软删除该 worker 的简历与对话日志
  - `user.status` 标记为 `deleted`
  - 硬删除交给 Phase 7 TTL / cleanup 任务
- 删除流程由 `user_service` 作为统一编排入口，内部组合调用 `conversation_service`、ORM 读写和审计 helper
- 删除操作必须写 `conversation_log`，并写 `audit_log`
- `/找岗位`、`/找工人` 只影响中介账号；非中介调用返回固定失败文案
- `/我的状态` 返回账号状态和最近一次提交状态，不做复杂聚合报表

### 5.9 测试要求

本阶段至少需要覆盖：

- 单元测试：
  - `user_service`
  - `intent_service`
  - `conversation_service`
  - `audit_service`
  - `permission_service`
  - `upload_service`
  - `search_service`
  - prompt 业务版定稿验证
- 集成测试：
  - 工人找岗位
  - 厂家找工人
  - 中介切换方向后检索
  - 岗位上传入库
  - 简历上传入库
  - `upload_service` + `search_service` 服务级串联 smoke
  - `/删除我的信息`
  - `show_more`
- 边界测试：
  - 空输入 / 表情 / 纯闲聊
  - LLM 非法 JSON fallback
  - session 过期
  - 候选条目失效
  - 宽松匹配只执行一次
- 合规测试：
  - 工人侧不泄漏电话、详细地址和歧视性字段
  - 待审/驳回条目不进入召回池
  - 删除流程会清空 session 并软删除相关数据

## 6. 交付物

Phase 3 完成后，后端至少应交付：

- `backend/app/services/user_service.py`
- `backend/app/services/intent_service.py`
- `backend/app/services/conversation_service.py`
- `backend/app/services/audit_service.py`
- `backend/app/services/permission_service.py`
- `backend/app/services/upload_service.py`
- `backend/app/services/search_service.py`
- `backend/app/llm/prompts.py` 的业务版更新
- 对应 `backend/tests/unit/` 与 `backend/tests/integration/` 自动化测试
- 一条可直接运行的 Phase 3 smoke 流程
- `backend/tests/README.md` 或等价文档更新

## 7. 前端需要做什么

本阶段前端无正式开发任务。

说明：

- Phase 3 只建设后端 service 层能力，不输出前端页面
- 如 Phase 3 过程中发现字段命名、详情链接、展示样式会影响后续前端契约，应在进入 Phase 5/6 前通过 handoff 文档同步

## 8. 验收标准

- 后端可用脚本或集成测试独立模拟完整业务服务流程
- 工人、厂家、中介三类典型流程均可在不依赖企微的前提下跑通
- prompt 已完成业务版定稿，且结构、few-shot、版本记录、fallback 行为均有自动化测试覆盖
- `criteria_patch` merge 与 `candidate_snapshot` 翻页行为正确
- `show_more` 复用快照，不重新执行全量检索
- 工人侧结果不泄漏电话、详细地址和歧视性字段
- 待审/驳回条目不会进入召回池
- `/删除我的信息` 流程可独立执行并通过自动化测试验证
- 本阶段代码没有越界实现到 webhook / worker / admin API / 前端页面

## 9. 风险与备注

### 9.1 结构化 key 的中英文歧义

方案设计中的示例部分大量使用中文字段名，但当前 ORM、schema 和 provider 输出契约都更适合英文 canonical key。本阶段统一锁定为“代码内一律英文 key，展示文案再做中文化”。

### 9.2 最终回复文本的安全边界

如果直接透传 `Reranker.reply_text`，工人侧可能泄漏电话、详细地址或歧视性字段。因此本阶段要求最终回复文本必须基于权限过滤后的结构化数据重新组装。

### 9.3 宽松匹配边界

方案设计中提到“薪资下调 10%，扩邻近城市”，但当前数据层没有可直接复用的邻近城市映射。本阶段先锁定“只做薪资放宽一次”，避免开发和测试各自猜测。

### 9.4 完成后需要做的事情

- 将本文件状态更新为 `done`
- 记录实际完成日期
- 补一份给 Phase 4 的 handoff，明确 message_router 该如何调用各 service
