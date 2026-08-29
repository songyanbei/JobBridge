# 01 架构总览

> 读者：产品、架构、后端、AI、测试和运维。
> 目标：说明系统为什么这样设计，以及各模块的责任边界。

## 1. 业务定位

JobBridge 是一个企业微信文字分类信息平台。用户通过自然语言完成信息发布、搜索、推荐和联系；招聘是第一个领域，二手物品是后续扩展的验证领域。

不同领域共享以下用户动作：

```text
发布信息 -> 补齐字段 -> 确认 -> 审核 -> 发布
搜索信息 -> 条件修改 -> 推荐 -> 更多 -> 联系
```

因此，业务流程应抽象为平台公共 Listing Flow，领域差异通过 Profile 表达。

## 2. 设计目标

- 提升对自然语言、口语化表达和多轮修改的理解能力；
- 覆盖现有岗位发布、简历发布、找岗位、找工人全部能力；
- 新增二手物品等标准领域时减少重复流程代码；
- 保证权限、审核、幂等、事务和数据脱敏可验证；
- 保持现有企业微信入口、Worker、MySQL、Redis 和运营后台兼容；
- 支持模型、Skill、Profile 的版本、灰度、回放和回滚。

## 3. 非目标

- 不让模型直接访问 SQL、Redis、对象存储或企微 API；
- 不采用模型自由规划所有流程的开放式 Agent；
- 不用一个包含大量 nullable 字段的超级表替代领域模型；
- 不在首期引入 Dify、LangGraph 或 Temporal 取代现有业务内核；
- 不在缺乏行为数据时建设跨领域统一推荐模型。

## 4. 总体架构

```text
┌───────────────────────────────────────────────────────────┐
│ Channel Adapter                                           │
│ WeCom Webhook / Mock WeCom / Mini Program                 │
└───────────────────────────┬───────────────────────────────┘
                            ▼
┌───────────────────────────────────────────────────────────┐
│ Durable Messaging                                         │
│ Inbox · claim/lease · user serial lock · retry · DLQ       │
└───────────────────────────┬───────────────────────────────┘
                            ▼
┌───────────────────────────────────────────────────────────┐
│ Conversation Runtime                                      │
│ command fast path · profile resolver · skill resolver      │
│ LLM parser · policy reducer · bounded orchestration        │
└───────────────────────────┬───────────────────────────────┘
                            ▼
┌───────────────────────────────────────────────────────────┐
│ Listing Runtime                                            │
│ publish · search · recommend · show_more · contact         │
│ draft · confirm · moderation · expiry · report             │
└───────────────────────────┬───────────────────────────────┘
                            ▼
┌───────────────────────────────────────────────────────────┐
│ Profile Registry + MCP Capability Adapter                  │
│ recruitment.job/resume · secondhand.item · ...             │
└───────────────────────────┬───────────────────────────────┘
                            ▼
┌───────────────────────────────────────────────────────────┐
│ Domain Services                                            │
│ permission · moderation · listing · search · ranking        │
│ audit · notification · account · admin                     │
└───────────────────────────┬───────────────────────────────┘
                            ▼
                   MySQL / Redis / Object Storage
```

## 5. 一条消息的执行顺序

1. Webhook 验签、解密、幂等后写入 durable Inbox 并立即返回企微。
2. Worker claim 消息并取得同用户串行锁。
3. 服务端构造 `ActorContext`，读取 Session、Profile、草稿和必要的历史摘要。
4. 显式命令、消息类型和当前状态硬约束优先；命中时不调用 LLM。
5. LLM 输出结构化 `DialogueParseResult` 或有限 `ActionProposal`。
6. Policy Reducer 校验 Profile、字段、角色、状态和风险等级。
7. Listing Runtime 调用固定 Action，必要时通过本地 MCP Adapter 调用领域服务。
8. 在事务中保存业务结果、Session、对话日志、Tool Trace 和 Outbox 事件。
9. 事务提交后发送企微消息；出站失败进入重试，不回滚已完成业务。

## 6. 三种运行模式

| 模式 | 模型职责 | 适用场景 |
|---|---|---|
| `strict_flow` | 只理解语义，不选任意 Tool | 发布、删除、审核、封禁、联系方式 |
| `config_flow` | 根据 Profile 选择有限动作 | 招聘、二手物品等标准 Listing |
| `bounded_orchestration` | 生成短计划，执行器限制动作/步数 | 组合搜索、筛选、推荐解释 |

生产环境不提供 `open_agent` 模式。

## 7. 责任边界

| 组件 | 负责 | 不负责 |
|---|---|---|
| LLM | 领域/意图/字段理解、语言表达 | 权限、审核、数据库写入 |
| Skill | 词汇、示例、追问和回复策略 | 安全边界和业务真相 |
| Flow Runtime | 合法状态迁移和通用流程 | 领域数据细节 |
| Profile | 字段、角色、审核、排序和索引配置 | 任意运行时逻辑 |
| MCP | 受控能力协议 | 事务和授权本身 |
| Domain Service | 查询、发布、审核、幂等和事务 | 解释用户自然语言 |
| Admin API | 稳定运营管理契约 | Agent 编排 |

## 8. 关键决策

### ADR-001：统一 Listing Flow

接受。发布、搜索、推荐和联系的交互骨架跨领域复用，公共 Runtime 能减少重复代码。

### ADR-002：Profile 而非超级表

接受。公共 Listing 字段稳定，领域属性、校验和索引有真实差异，采用公共主体 + 领域详情。

### ADR-003：有界模型编排

接受。低风险场景保留自然语言灵活性，高风险操作仍由确定性流程执行。

### ADR-004：MCP 首期本地化

接受。协议先统一能力契约，暂不引入远程网络和微服务运维开销。
