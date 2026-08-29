# 05 迁移、测试与管理后台兼容

> 读者：项目负责人、后端、前端、测试、运维和运营。
> 目标：定义从当前系统到新架构的可回滚实施路径。

## 1. 迁移原则

- 适配器先行，不一次性重写；
- 只读搜索先行，写入发布后迁移；
- 现有 Domain Service、数据表和后台先保持不变；
- 新旧路径同时回放和比较；
- 每个阶段具备独立验收和回退开关；
- 新功能不得继续膨胀 `message_router.py`。

## 2. 首期数据迁移方案

首期招聘不新增公共 `listing` 事实表，采用 Facade：

```text
recruitment.job:{job.id}       -> jobs（事实源）
recruitment.resume:{resume.id} -> resumes（事实源）
```

Phase 0 的数据库迁移为 `jobs`、`resumes` 增加 `aggregate_version`（历史行回填为 1）；之后所有 Service 更新必须在同一事务内按行锁/CAS 递增版本。`domain_outbox_event` 的完整字段和消费者恢复语义以 [01 架构总览 §5.1](01-architecture-overview.md) 为准。

新 Listing Runtime 的读写都经 Facade 调用现有岗位/简历 Service；不对现有业务表和新表做双写。每次旧表事务同时写 `domain_outbox_event`，Indexer 以 `event_id` 幂等更新搜索索引。事件的字段、状态、lease、重试和 DLQ 契约以 [01 架构总览 §5.1](01-architecture-overview.md) 为准；索引落后或不可用时回退旧 SQL，查询返回前仍重新校验状态、审核、权限和有效期。

旧 `Job/Resume Service` 必须把创建、字段更新、审核状态变化、过期、下架和删除统一收口到同一个事务 helper：先更新事实源并递增 `aggregate_version`，再插入对应 `domain_outbox_event`。任何绕过 Service 的后台脚本、定时任务或数据修复都必须调用同一 helper，否则不得宣布索引一致性完成。

二手物品试点才新增 `listing` + `listing_detail_item`，该领域以新表为事实源，使用 `secondhand.item:{listing.id}` 作为不透明引用。不能用裸数字 ID 跨领域关联。

回填和切换要求：

1. 按主键分页回填索引，保存 checkpoint，可暂停和重跑；
2. 回填结束后执行数量、状态、抽样字段和权限过滤校验；
3. 先 shadow read 比较旧 SQL 与新索引结果，再按 Profile/用户灰度；
4. 招聘回滚只切换读取/执行路由，旧表不需要数据回滚；
5. 二手回滚停止新写入和对外读取，保留数据供后台核查，不做破坏性删除。

## 3. 目标代码结构

```text
backend/app/
├── conversation/
│   ├── runtime.py
│   ├── session.py
│   ├── commands.py
│   ├── reducer.py
│   └── policies.py
├── listing/
│   ├── runtime.py
│   ├── profiles.py
│   ├── schemas.py
│   ├── publish.py
│   ├── search.py
│   ├── recommend.py
│   ├── contact.py
│   └── render.py
├── domains/
│   ├── recruitment/
│   │   ├── profiles.py
│   │   ├── matching.py
│   │   └── policies.py
│   └── secondhand/
│       ├── profiles.py
│       └── policies.py
├── agent/
│   ├── llm_gateway.py
│   ├── parser.py
│   ├── bounded_orchestrator.py
│   ├── skill_registry.py
│   └── trace.py
├── mcp/
├── messaging/
└── services/legacy_router_adapter.py
```

现有 `search_service`、`upload_service`、`dialogue_reducer/applier` 可以先通过 adapter 接入，不要求一次完成物理搬迁。

## 4. 分阶段计划

### Phase 0：基线和回放集（1 周）

- 冻结岗位、简历、找岗位、找工人的 golden cases；
- 记录 legacy/v2 行为差异；
- 固化角色权限、后台 API 和幂等测试；
- 修正当前“限流通知在 `wecom_inbound_event` 前直接丢弃”的实现：扩展该表的 `turn_id` 和 `rate_limit_decision` 字段；正常消息保持 `status=received`，限流消息写入审计后使用 `status=done, rate_limit_decision=rate_limited`；
- 保持旧 Worker 可识别的 status 闭集 `received/processing/session_pending/done/failed/dead_letter`；Redis 队列故障由 dispatcher 扫描 `status=received AND rate_limit_decision=accepted` 补投；
- 增加模型、Skill、Profile 和 schema 版本日志。

### Phase 1：协议和状态（1~2 周）

- 统一 `DialogueParseResult`；
- 实现 `ActorContext`、`SessionCommand`、`PolicyValidator`；
- 给 Session 增加 `schema_version`、`version`、`profile`；
- 让现有 reducer/applier 作为兼容实现。

### Phase 2：Listing Runtime（2 周）

- 抽取通用草稿、字段收集、确认、取消、过期、搜索快照；
- 实现 Profile Registry；
- 实现 `ListingCard` 和公共回复渲染器；
- 以现有岗位/简历表为事实源，不做招聘业务双写；
- 同事务写 `domain_outbox_event`，构建索引同步链路；
- 新 Session 生成 legacy compatibility projection，支持回退读取。

### Phase 3：搜索迁移（1~2 周）

- 封装 `listing.search`、`listing.show_more`；
- 先迁移找岗位、找工人；
- 保留硬过滤、重排、脱敏、快照和放宽；
- 新旧路径 shadow compare 后再灰度。

### Phase 4：发布迁移（2~3 周）

- 迁移岗位和简历发布；
- 覆盖图片、冲突、TTL、审核、确认和发布后搜索；
- 启用写操作幂等键和 Outbox；
- 先对内部测试用户开放。

### Phase 5：二手物品试点（2~4 周）

- 增加 `secondhand.item` Profile、Schema、字典、索引和 Skill；
- 复用 Listing Runtime；
- 以“不修改招聘核心 Flow”作为扩展验收条件。

### Phase 6：主路由切换（持续）

- 按用户、角色、Profile 灰度；
- 稳定后 legacy 降为 fallback；
- 只有存在多个外部客户端时才远程化 MCP。

## 5. 回滚和进行中请求处理

### 5.1 路由回滚

新 Runtime 通过 `listing_runtime_enabled`、Profile 和用户灰度开关控制。触发停止条件后：

1. 停止把新消息分配给新 Runtime；
2. 允许已开始的 turn 在 30 秒内完成；
3. 超时 turn 由 `wecom_inbound_event` lease 恢复，先查询 `action_execution` 再决定是否重试；
4. 新消息转给 `legacy_router_adapter`；
5. 记录回滚事件、原因、版本和影响范围。

### 5.2 Session 兼容

新 Session 使用版本化结构，但招聘灰度期间必须维护 legacy compatibility projection：

- 新 Runtime 提交状态时同步生成旧字段映射；
- legacy 读取不到新字段时由 adapter 从旧字段恢复；
- 只允许招聘 Profile 做该兼容投影；
- 二手 Profile 不回退到招聘 Session，停写后由后台处理。

### 5.3 数据和 Outbox 兼容

- 招聘业务事实仍在 `jobs/resumes`，已提交动作无需反向回滚；
- `action_execution` 记录是写动作唯一执行凭据；
- 已提交 Outbox 不因路由回滚而取消，发送器继续按唯一键重试；
- 未提交事务整体回滚，不产生半成品；
- 索引事件可重复消费，最终以事实源版本覆盖旧文档。

## 6. 测试设计

### 4.1 单元和契约测试

- Profile 字段、枚举、字典、索引和权限；
- DialogueParseResult 和 ActionProposal schema；
- Policy Reducer 状态迁移；
- Session CAS 和版本冲突；
- MCP Tool 输入、错误码和幂等；
- 搜索分页、快照、脱敏和卡片渲染；
- `wecom_inbound_event`/Outbox/retry/dead-letter。

### 4.2 对话回放

每个 Profile 建立 golden cases：

- 一句话完整发布；
- 多轮补字段；
- 字段替换、追加和删除；
- 中途取消、重新开始和流程冲突；
- 搜索条件追加和修改；
- 无结果、低召回和“更多”；
- 越权、脱敏和 Prompt Injection；
- LLM 超时、解析失败和 fallback。

### 4.3 二手领域验收

增加二手物品后必须满足：

- 招聘四条核心 Flow 回放结果不变；
- 不新增招聘 Router 分支；
- 不影响现有后台岗位/简历页面；
- 新领域仅通过 Profile、Schema、字典、索引和 Skill 接入；
- 公共状态、幂等、审计和推荐卡片逻辑可复用。

## 7. 管理后台兼容

后台不改成 Agent，也不由 MCP 取代 Admin API。继续保留：

- Vue 3、Element Plus、Vite、Pinia；
- `/admin/*` 路径、JWT、首次改密和统一响应；
- 厂家、中介、工人、黑名单管理；
- 岗位、简历查询、编辑、下架、延期、恢复和导出；
- 审核工作台 lock/version/pass/reject/edit/undo；
- 城市、工种、品类、敏感词和系统配置；
- Dashboard、趋势、TOP、漏斗、日志和事件回传。

只增加兼容字段，例如 `profile`、`listing_type`、`trace_id`、`skill_version`，不改变现有页面路径和核心交互。

## 8. 上线门槛

- 关键权限和高风险操作测试 100% 通过；
- 写操作重复执行不产生重复实体；
- LLM 不可用时命令、草稿恢复和后台仍可用；
- 新旧路径可按用户回退；
- 每次运行可追踪模型、Skill、Profile、Tool 和状态版本；
- `wecom_inbound_event` backlog、Outbox 失败和死信都有告警；
- 二手试点不引入招聘行为回归。

具体数值门槛和停止条件以 [04 可靠性与性能](04-reliability-performance-and-observability.md) 为准；没有达到门槛时只允许 shadow 或小范围灰度，不允许扩大生产流量。
