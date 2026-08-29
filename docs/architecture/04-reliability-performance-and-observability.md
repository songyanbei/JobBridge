# 04 可靠性、性能与可观测性

> 读者：后端、测试、运维和安全。
> 目标：保证引入 LLM 后仍然具备可恢复、可追踪和可扩展的生产特性。

## 1. 消息处理可靠性

```text
企微 Webhook
  -> 验签/解密
  -> wecom_inbound_event 持久化（唯一键去重）
  -> 限流判定
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
- 入队失败不能静默丢消息，必须保留 `wecom_inbound_event.status` 状态。

### 1.1 限流语义和 Redis 故障

有效企微消息在限流判定前先写入现有 `wecom_inbound_event`。协议非法、验签失败的请求直接拒绝，不写入站表。超过业务限流的消息可以不进入 Worker，以保护系统，但必须保留一条 `wecom_inbound_event` 记录，使用现有终态 `status=done`，并设置 `rate_limit_decision=rate_limited`、规则版本和原因；默认不自动重放，运营人员可按审计记录人工选择重放原始消息。

当前入站表和状态以 `backend/app/models.py::WecomInboundEvent` 为准：表名为 `wecom_inbound_event`，状态闭集为 `received/processing/session_pending/done/failed/dead_letter`。目标不引入名为 `wecom_inbox` 的并行表，也不把 `accepted` 或 `rate_limited` 当作 `status` 值。

Phase 0 只对该表做向后兼容扩展：增加 `turn_id`（唯一）、`rate_limit_decision`（`accepted/rate_limited`）、`rate_limit_rule` 和 `rate_limited_at`；保留原有 status 枚举。正常可处理消息保持 `status=received, rate_limit_decision=accepted`，限流消息在审计写入后置为 `status=done, rate_limit_decision=rate_limited`，旧 Worker 会按现有终态忽略它。人工重放不复用已完成 turn，而是创建关联原记录的新 turn_id/重放记录，避免 action 幂等键冲突。

Redis 故障时分层处理：

1. **数据库可用、Redis 队列不可用**：Webhook 在 `wecom_inbound_event` 提交后返回成功；后台 dispatcher 定期扫描 `status=received AND rate_limit_decision=accepted` 的记录补投队列，不能丢消息。
2. **Redis 限流不可用**：限流采用进程内保守阈值，按 fail-closed 处理未知状态，不把故障当作无限流量；超过保守阈值的消息写为 `status=done, rate_limit_decision=rate_limited`，不发送重复通知。
3. **wecom_inbound_event 数据库不可用**：接入 fail-closed，返回非 2xx 让企微重试；禁止在无法持久化时返回成功。

限流提示本身是非关键出站消息，可以丢弃或不重试，但提示投递结果和丢弃原因必须写审计，不得影响业务消息 `wecom_inbound_event`。

以上是目标接入契约，不代表当前代码已经满足。当前 Webhook/限流实现仍可能在 `wecom_inbound_event` 写入前处理限流通知；该差异必须在上线前置 Phase 0 完成，并以“有效消息先落 `wecom_inbound_event`、使用现有 status 闭集、限流记录可审计、Redis/数据库故障行为符合本节”为验收标准。未完成前不得将当前实现标记为 durable Inbox 一致。

## 2. 业务幂等和事务

每条入站消息在写入 `wecom_inbound_event` 时生成不可变 `turn_id`（推荐 UUID/ULID）；重试和超时恢复复用原 `turn_id`，人工重放创建新的关联 turn。`conversation_id` 由用户会话稳定生成，例如 `wecom:{external_userid}`，不能由模型生成。

统一增加 `action_execution` 记录：

```text
action_execution
  id, turn_id, action_name, status
  request_digest, result_digest
  lease_owner, lease_until, fencing_token
  created_at, finished_at

unique(turn_id, action_name)
```

`idempotency_key` 固定为 `turn_id + action_name`，而不是每次重试重新生成。执行语义为：

- `started` 且 lease 未过期：不并发执行，等待原执行者；
- `started` 且 lease 已过期：同一 key 可被恢复执行；
- `succeeded`：直接返回已保存结果，只补发未成功的 Outbox；
- `failed_retryable`：复用同一 key 按退避策略重试；
- `failed_terminal`：不再自动重试，进入人工关注。

最小 claim/update 条件如下：

```sql
-- 首次创建或抢占已过期执行
INSERT INTO action_execution
  (turn_id, action_name, status, lease_owner, lease_until, fencing_token)
VALUES (:turn_id, :action, 'started', :worker, :until, 1)
ON DUPLICATE KEY UPDATE
  lease_owner = CASE WHEN status = 'started' AND lease_until < NOW()
                     THEN :worker ELSE lease_owner END,
  lease_until = CASE WHEN status = 'started' AND lease_until < NOW()
                     THEN :until ELSE lease_until END,
  fencing_token = CASE WHEN status = 'started' AND lease_until < NOW()
                       THEN fencing_token + 1 ELSE fencing_token END;

-- 只有当前 owner 和 fencing token 可以提交
UPDATE action_execution
SET status = 'succeeded', result_digest = :digest, finished_at = NOW()
WHERE turn_id = :turn_id AND action_name = :action
  AND status = 'started'
  AND lease_owner = :worker
  AND fencing_token = :token;
```

业务写入、Session CAS 和 `action_execution=succeeded` 必须在同一数据库事务中完成；更新影响行数为 0 时，当前 Worker 不得提交业务结果。两个 Worker 同时恢复时，只有成功 claim 的 `lease_owner + fencing_token` 能继续，旧 Worker 即使晚到也会因 fencing 条件失败而回滚。若业务写入提交后进程超时，重试先读取 `action_execution` 和业务唯一键，只补发 Outbox，不重新创建实体。数据库唯一约束、业务状态检查和 Session version/CAS 共同防止：

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
- `wecom_inbound_event` backlog、lease 超时、重试、死信；
- Outbox 发送成功率、重复发送率和积压；
- 发布重复率、搜索无结果率、低召回接受率；
- 联系点击率、回复率和领域转化率；
- 各模型、Skill、Profile 版本的成本和回退率。

## 7. 核心 SLO 和停止条件

### 7.1 目标 SLO（首期生产）

| 指标 | 目标 |
|---|---:|
| 有效入站消息持久化成功率 | >= 99.99% |
| 消息丢失率（已验签有效消息） | 0 |
| 普通搜索端到端 P95 | <= 5 秒 |
| 命令/更多/取消 P95 | <= 1.5 秒 |
| Outbox 最终发送成功率 | >= 99.9% |
| Tool 5xx 比例 | < 0.5% |
| 非法 Flow 迁移 | 0 |
| 高风险写操作重复率 | 0 |
| 跨用户数据/联系方式泄露 | 0 |
| `wecom_inbound_event` 最老未处理消息 | < 60 秒 |

### 7.2 灰度停止/回滚条件

连续 15 分钟或累计 1000 条新 Runtime 请求中，满足任一条件立即停止扩大流量并切回 legacy：

- 发现任意一次跨用户数据、联系方式或权限泄露；
- 发现任意一次重复发布、重复删除或绕过审核的高风险写入；
- 普通搜索 P95 > 8 秒；
- LLM fallback 率 > 10%；
- Tool 5xx > 2%；
- `wecom_inbound_event` 最老消息 > 120 秒或积压持续增长；
- Outbox 失败率 > 1% 或死信超过 20 条/小时；
- 非法状态迁移 > 0.1%。

回滚只关闭新 Runtime 的分流开关，不删除已提交业务数据；未完成 Action 由 `action_execution` 和 lease 恢复，已提交 Outbox 继续按原幂等键发送。

## 8. 扩展和容量策略

早期继续使用 MySQL + Redis + SQL 硬过滤。数据量和行为数据达到阈值后，再按需引入：

- OpenSearch/Elasticsearch：全文检索和地理距离；
- 向量召回：扩大语义召回，不替代权限和硬过滤；
- 特征平台和离线排序模型；
- 独立 MCP Server；
- Temporal：跨天、人工等待或交易流程。

扩容顺序应由 backlog、P95、数据库负载和搜索召回指标驱动，而不是由 Agent 架构预先决定。
