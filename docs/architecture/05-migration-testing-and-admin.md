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

## 2. 目标代码结构

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

## 3. 分阶段计划

### Phase 0：基线和回放集（1 周）

- 冻结岗位、简历、找岗位、找工人的 golden cases；
- 记录 legacy/v2 行为差异；
- 固化角色权限、后台 API 和幂等测试；
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
- 不改变现有岗位/简历表和后台接口。

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

## 4. 测试设计

### 4.1 单元和契约测试

- Profile 字段、枚举、字典、索引和权限；
- DialogueParseResult 和 ActionProposal schema；
- Policy Reducer 状态迁移；
- Session CAS 和版本冲突；
- MCP Tool 输入、错误码和幂等；
- 搜索分页、快照、脱敏和卡片渲染；
- Inbox/Outbox/retry/dead-letter。

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

## 5. 管理后台兼容

后台不改成 Agent，也不由 MCP 取代 Admin API。继续保留：

- Vue 3、Element Plus、Vite、Pinia；
- `/admin/*` 路径、JWT、首次改密和统一响应；
- 厂家、中介、工人、黑名单管理；
- 岗位、简历查询、编辑、下架、延期、恢复和导出；
- 审核工作台 lock/version/pass/reject/edit/undo；
- 城市、工种、品类、敏感词和系统配置；
- Dashboard、趋势、TOP、漏斗、日志和事件回传。

只增加兼容字段，例如 `profile`、`listing_type`、`trace_id`、`skill_version`，不改变现有页面路径和核心交互。

## 6. 上线门槛

- 关键权限和高风险操作测试 100% 通过；
- 写操作重复执行不产生重复实体；
- LLM 不可用时命令、草稿恢复和后台仍可用；
- 新旧路径可按用户回退；
- 每次运行可追踪模型、Skill、Profile、Tool 和状态版本；
- Inbox backlog、Outbox 失败和死信都有告警；
- 二手试点不引入招聘行为回归。
