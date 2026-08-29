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
│ wecom_inbound_event · claim/lease · user lock · retry · DLQ │
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

## 5. 数据事实源和迁移策略

首期不强行把现有 `jobs`/`resumes` 表迁移成新的公共 `listing` 表。招聘领域采用 **Listing Facade**：

```text
recruitment.job:{job.id}      -> jobs（事实源）
recruitment.resume:{resume.id} -> resumes（事实源）
```

Facade 对上提供统一的 `ListingCard`、Profile 和 Flow 接口，对下仍调用现有 `upload_service`、`search_service` 和管理后台 Service。首期不对岗位/简历做业务双写，避免两套实体状态不一致。

数据一致性路径固定为：

```text
领域事务（旧表）
  + domain_outbox_event（同一事务写入）
  -> indexer 按 event_id 幂等更新搜索索引
  -> 查询时再次校验 status、moderation_status、权限和 expires_at
```

索引是派生数据，不是事实源。索引延迟或丢失时，系统可以降级到旧 SQL 搜索，不能返回已下架或越权数据。

二手物品作为第二领域试点时，可以新增 `listing` + `listing_detail_item`，其自身表为事实源；Facade 统一暴露两类来源。两类来源都使用不透明的 `listing_ref`，不能用可能冲突的裸数字 ID。

切换和回滚：招聘先按用户/ Profile 灰度，读取和写入均通过 Facade；回滚只切换 Conversation Runtime 到 legacy adapter，不需要回滚业务数据，因为旧表仍是事实源。新运行时提交成功的岗位/简历可被 legacy 直接读取。二手物品试点回滚时停止新 Runtime 的写入和对外读取，保留其数据供后台核查，不执行破坏性删除。

### 5.1 `domain_outbox_event` 最小契约

```text
domain_outbox_event
  event_id           UUID/ULID，主键，全局唯一
  aggregate_type     job | resume | item
  aggregate_id       业务表主键（字符串化）
  aggregate_version  事实源行版本，单调递增
  event_type         created | updated | submitted | published |
                     rejected | expired | delisted | deleted
  payload            脱敏后的索引字段快照，不含 phone/微信号
  status             pending | processing | succeeded | retryable | dead_letter
  attempts           重试次数
  available_at       下次可消费时间
  locked_by          consumer lease owner
  locked_until       consumer lease 到期时间
  created_at / processed_at / last_error
```

首期招聘迁移需给现有 `jobs`、`resumes` 增加 `aggregate_version BIGINT NOT NULL DEFAULT 1`（历史数据统一回填为 1），并在 Service 的更新事务中以行锁/CAS 递增；二手 `listing` 表从创建时即维护该字段。

约束和恢复语义：

- `event_id` 主键唯一；同一 aggregate 的 `aggregate_version` 必须唯一，防止同一版本重复发事件；
- 旧 `Job/Resume Service` 的每个创建、字段更新、审核状态变化、过期、下架和删除路径必须在同一数据库事务中插入事件；禁止由 Router 事后补写；
- `created/updated/submitted/published/rejected/expired/delisted` 事件的 payload 只用于索引提示，消费者必须回源读取事实源并再次校验状态，不能把 payload 当最终业务数据；
- `deleted` 是 tombstone 例外：物理删除后事实源可能不存在，Indexer 只依据来自受信任事务事件的 `aggregate_type + aggregate_id + aggregate_version + event_type=deleted` 删除/隐藏索引，并写入保留期内的 tombstone 版本；不要求回源；
- `expired/delisted` 是软不可见：事实源仍保留，Indexer 回源确认版本后从公开检索集合移除，但保留可恢复/后台查询所需的文档或状态；未来重新激活必须产生更高 `aggregate_version` 的 `published` 事件；
- Indexer 以 `event_id` 和 `(aggregate_type, aggregate_id, aggregate_version)` 幂等消费，重复事件直接标记成功；
- `pending/processing` 使用 lease，超时回收为 `retryable`；指数退避超过上限进入 `dead_letter`；
- DLQ 事件保留原始错误和 payload 摘要，支持修复后按原 `event_id` 重放；
- 索引文档携带 `aggregate_version`；Indexer 维护 `(aggregate_type, aggregate_id) -> deleted_version`，任何版本小于等于 tombstone 的旧 `created/updated/published` 事件都丢弃并记录，防止删除后旧事件复活。tombstone 至少保留到事件重放窗口结束。

## 6. 一条消息的执行顺序

1. Webhook 验签、解密、幂等后写入 `wecom_inbound_event` 并立即返回企微。
2. Worker claim 消息并取得同用户串行锁。
3. 服务端构造 `ActorContext`，读取 Session、Profile、草稿和必要的历史摘要。
4. 显式命令、消息类型和当前状态硬约束优先；命中时不调用 LLM。
5. LLM 输出结构化 `DialogueParseResult` 或有限 `ActionProposal`。
6. Policy Reducer 校验 Profile、字段、角色、状态和风险等级。
7. Listing Runtime 调用固定 Action，必要时通过本地 MCP Adapter 调用领域服务。
8. 在事务中保存业务结果、Session、对话日志、Tool Trace 和 Outbox 事件。
9. 事务提交后发送企微消息；出站失败进入重试，不回滚已完成业务。

## 7. 三种运行模式

| 模式 | 模型职责 | 适用场景 |
|---|---|---|
| `strict_flow` | 只理解语义，不选任意 Tool | 发布、删除、审核、封禁、联系方式 |
| `config_flow` | 根据 Profile 选择有限动作 | 招聘、二手物品等标准 Listing |
| `bounded_orchestration` | 生成短计划，执行器限制动作/步数 | 组合搜索、筛选、推荐解释 |

生产环境不提供 `open_agent` 模式。

## 8. 责任边界

| 组件 | 负责 | 不负责 |
|---|---|---|
| LLM | 领域/意图/字段理解、语言表达 | 权限、审核、数据库写入 |
| Skill | 词汇、示例、追问和回复策略 | 安全边界和业务真相 |
| Flow Runtime | 合法状态迁移和通用流程 | 领域数据细节 |
| Profile | 字段、角色、审核、排序和索引配置 | 任意运行时逻辑 |
| MCP | 受控能力协议 | 事务和授权本身 |
| Domain Service | 查询、发布、审核、幂等和事务 | 解释用户自然语言 |
| Admin API | 稳定运营管理契约 | Agent 编排 |

## 9. 关键决策

### ADR-001：统一 Listing Flow

接受。发布、搜索、推荐和联系的交互骨架跨领域复用，公共 Runtime 能减少重复代码。

### ADR-002：Profile 而非超级表

接受。公共 Listing 字段稳定，领域属性、校验和索引有真实差异，采用公共主体 + 领域详情。

### ADR-003：有界模型编排

接受。低风险场景保留自然语言灵活性，高风险操作仍由确定性流程执行。

### ADR-004：MCP 首期本地化

接受。协议先统一能力契约，暂不引入远程网络和微服务运维开销。
