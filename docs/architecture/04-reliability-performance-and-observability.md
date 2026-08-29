# 04 可靠性、性能与可观测性

> 读者：后端、测试、运维和安全。
> 目标：保证引入 LLM 后仍然具备可恢复、可追踪和可扩展的生产特性。

## 1. 消息处理可靠性

```text
企微 Webhook
  -> Inbox 唯一键去重
  -> Worker claim + lease
  -> 用户串行锁
  -> 业务事务
  -> ack
```

要求：

- `wecom_msg_id` 唯一键去重；
- Worker claim 使用租约，处理完成后确认；
- lease 超时自动恢复；
- 明确最大重试次数、指数退避和死信队列；
- 同一用户继续串行处理，避免 Session 覆盖；
- 入队失败不能静默丢消息，必须保留 Inbox 状态。

## 2. 业务幂等和事务

每个写 Action 使用：

```text
idempotency_key = conversation_id + turn_id + action_name
```

数据库唯一约束、业务状态检查和 Session version/CAS 共同防止：

- 重复发布岗位或物品；
- 重复创建简历；
- 重复挂载附件；
- 重复提交审核；
- 同一轮消息重复发送。

## 3. Outbox 出站

不要在业务事务中同步依赖企微网络：

```text
业务写入 + outbox_message
  -> commit
  -> sender 投递企微
  -> 失败重试
  -> 超限进入人工关注/死信
```

企微发送的去重键必须独立于业务写入幂等键，避免业务成功但回复失败时重复发布。

## 4. 搜索性能路径

普通找岗位/找工人使用快速路径：

```text
Session/角色确定 Profile
  -> 一次结构化 LLM 解析
  -> Schema/Policy 校验
  -> SQL 硬过滤
  -> Top-N 重排
  -> 固定 ListingCard 回复
```

不做单独的领域分类调用，不让模型生成搜索计划，不让模型读取全集候选。MCP 首期采用本地 adapter，Profile 和 Skill 预加载。

### 4.1 调用预算

| 场景 | LLM 调用 | Tool 调用 | 说明 |
|---|---:|---:|---|
| 取消、重置、更多、显式命令 | 0 | 1 | 纯后端快速响应 |
| 普通 Listing 搜索 | 1 | 1~2 | 不增加规划调用 |
| 发布补字段 | 每轮 1 | 1 | 草稿保存状态 |
| 低召回确认 | 1 | 1 | 预定义放宽步骤 |
| 跨领域组合搜索 | 最多 2 | 最多 4 | 有界编排 |

默认单轮总时限 20~25 秒；超过预算返回固定兜底。具体 P50/P95 目标需按实际 Provider 压测确认。

### 4.2 优化手段

1. 只传当前 Profile 和必要历史摘要；
2. 城市、工种、品类字典本地缓存；
3. SQL 先过滤，重排只处理 Top 20~50；
4. 普通推荐使用模板，LLM 文案润色异步或可选；
5. “更多”消费候选快照，不重复全量重排；
6. Worker 横向扩容，按用户锁保证同用户串行；
7. LLM Provider 使用超时、熔断和降级模型。

## 5. 安全和隐私

- 权限以服务端 `ActorContext` 为准；
- Skill/Prompt 不能改变系统规则；
- 用户输入中的“忽略规则”等内容只作为普通文本；
- Tool 只返回 actor 有权访问的数据；
- 联系方式在服务端统一脱敏；
- 发布、审核、删除、封禁、举报和联系都写审计；
- 附件校验 owner、类型、大小和生命周期；
- 用户数据删除必须清理 Session、日志敏感字段和附件引用。

## 6. 可观测性

### 6.1 事件维度

每次对话至少记录：

```text
trace_id
conversation_id
turn_id
userid（按隐私策略脱敏）
profile
flow_state_before/after
model/provider/model_version
skill_version
parse_schema_version
tool_name/tool_status
latency/token_usage
fallback_reason
```

### 6.2 核心指标

- `llm_parse_success_rate` 和字段级准确率；
- `flow_transition_invalid_total`；
- Profile/Schema 校验失败率；
- Tool P50/P95/P99 和错误码；
- Inbox backlog、lease 超时、重试、死信；
- Outbox 发送成功率、重复发送率和积压；
- 发布重复率、搜索无结果率、低召回接受率；
- 联系点击率、回复率和领域转化率；
- 各模型、Skill、Profile 版本的成本和回退率。

## 7. 扩展和容量策略

早期继续使用 MySQL + Redis + SQL 硬过滤。数据量和行为数据达到阈值后，再按需引入：

- OpenSearch/Elasticsearch：全文检索和地理距离；
- 向量召回：扩大语义召回，不替代权限和硬过滤；
- 特征平台和离线排序模型；
- 独立 MCP Server；
- Temporal：跨天、人工等待或交易流程。

扩容顺序应由 backlog、P95、数据库负载和搜索召回指标驱动，而不是由 Agent 架构预先决定。
