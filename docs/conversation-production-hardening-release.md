# 对话生产加固发布说明

版本日期：2026-07-25
基线：`edd5183`
分支：`codex/conversation-production-hardening`

详细设计、基线审计和完整验收记录见
[conversation-production-hardening-plan.md](conversation-production-hardening-plan.md)。

## 1. 发布范围

本次覆盖以下纯文本对话链路：

- 岗位和简历发布；
- 多轮补字段、取消、草稿恢复和发布/搜索冲突切换；
- 岗位/工人搜索、推荐、翻页、自动放宽；
- 未见表达、角色方向约束和最多两个动作的组合意图；
- webhook、Redis 队列、多 Worker、MySQL 业务事务、Redis session 和企微回复。

图片、文件和语音能力没有扩展；原有兼容路径仍保留。

## 2. 核心变更

### 对话与搜索

- 非显式取消、成功发布或 TTL 到期不再清除发布草稿。
- upload/search 冲突连续无法确认时恢复原草稿，而不是丢弃草稿。
- `DialoguePlan` 最多接受两个明确动作；三个及以上动作进入澄清。
- worker、factory 和 broker 的搜索方向由后端权限与明确主客体锚点约束。
- 字典支持的城市、工种原文锚点可修正模型漏抽取。
- legacy、v2 和 v2 fallback 使用一致的本轮条件 provenance。
- reranker timeout、HTTP、解析和未知异常均退回稳定 SQL 顺序。
- 队列积压达到阈值时跳过非关键 rerank。
- provider 增加进程级 circuit breaker。

### 一致性与恢复

- Redis 用户锁自动续租，并以锁 token 对 session CAS 做 fencing。
- session 增加单调 `session_version`。
- 同一用户按持久化 `wecom_inbound_event.id` 顺序提交。
- webhook 先持久化入站事件再入 Redis；Redis 入队失败由数据库扫描恢复。
- MySQL 业务写、conversation log、session 提交意图和 outbox 在同一事务提交。
- Redis session 提交失败保持 `session_pending`，由 Worker 幂等恢复。
- 企微回复通过事务 outbox 投递，并保持同用户回复顺序。
- Worker crash、Redis/MySQL 短断和 stale claim 均有恢复路径。

### 运维

- DB/Redis 操作增加有界超时。
- 生产默认使用 JSON loguru 日志，用户标识以摘要写入日志。
- 增加 queue age、process latency、outbox、session commit 和 dead-letter 监控。
- TTL 清理只删除终态且没有未发送 outbox 的入站事件；可恢复事件不会因年龄被删除。
- Nginx 动态解析 app 容器地址，健康检查覆盖反向代理链路。

## 3. 数据库迁移

必须按以下顺序执行：

```bash
sh scripts/apply_sql_migration_compose.sh \
  backend/sql/migrations/phase8_001_conversation_recovery_indexes.sql
sh scripts/apply_sql_migration_compose.sh \
  backend/sql/migrations/phase8_002_inbound_event_microseconds.sql
sh scripts/apply_sql_migration_compose.sh \
  backend/sql/migrations/phase8_003_wecom_outbound_outbox.sql
sh scripts/apply_sql_migration_compose.sh \
  backend/sql/migrations/phase8_004_outbox_user_order_index.sql
sh scripts/apply_sql_migration_compose.sh \
  backend/sql/migrations/phase8_005_durable_session_commit.sql
```

五个迁移均按 MySQL 8.0 编写并可重复执行。应用新 Worker 前必须完成全部迁移；
否则新代码会访问不存在的状态值、列或 outbox 表。

## 4. 配置

从 `.env.example` 同步下列配置，并根据容量测试调整：

| 配置 | 默认值 | 说明 |
|---|---:|---|
| `DB_CONNECT_TIMEOUT_SECONDS` | 2 | DB 建连超时 |
| `DB_READ_TIMEOUT_SECONDS` | 5 | DB 读取超时 |
| `DB_WRITE_TIMEOUT_SECONDS` | 5 | DB 写入超时 |
| `DB_POOL_TIMEOUT_SECONDS` | 3 | 连接池等待超时 |
| `REDIS_CONNECT_TIMEOUT_SECONDS` | 1 | Redis 建连超时 |
| `REDIS_SOCKET_TIMEOUT_SECONDS` | 2 | 必须大于 Worker BLPOP 1 秒 |
| `LLM_CIRCUIT_FAILURE_THRESHOLD` | 5 | circuit 打开前连续失败数 |
| `LLM_CIRCUIT_RECOVERY_SECONDS` | 30 | half-open 等待时间 |
| `RERANKER_QUEUE_DEGRADE_THRESHOLD` | 10 | 达到该 inbound 深度时跳过 rerank |
| `MONITOR_QUEUE_INCOMING_MAX_AGE_SECONDS` | 120 | 最老队列消息告警 |
| `MONITOR_OUTBOX_PENDING_MAX_AGE_SECONDS` | 300 | outbox 超龄告警 |
| `MONITOR_SESSION_COMMIT_PENDING_MAX_AGE_SECONDS` | 300 | session commit 超龄告警 |

首次发布保持以下灰度值：

```dotenv
DIALOGUE_V2_MODE=off
DIALOGUE_V2_PRIMARY_ROLLOUT_PERCENTAGE=0
POST_SEARCH_POLICY_MODE=off
PHASE5_ROLLOUT_PERCENTAGE=0
```

不要同时提升多个开关。建议按 shadow → 5% → 25% → 50% → 100% 推进。

## 5. 部署顺序

1. 备份 MySQL，并确认 Redis 持久化/高可用状态。
2. 暂停新版本 Worker 扩容，执行五个数据库迁移。
3. 构建 app 和 worker 镜像。
4. 先滚动更新 app，再以少量新 Worker 启动。
5. 确认 `/health`、Worker heartbeat、入站队列和数据库迁移状态。
6. 执行真实服务烟测。
7. 扩到按 `C_peak` 计算的 Worker 数量，再执行负载和混沌验收。
8. 从 shadow 开始逐级放量对话策略。

测试覆盖文件 `docker-compose.hardening-test.yml` 会启用 `APP_ENV=test` 和
mock 企微出站，只能用于隔离测试环境，不得作为生产 Compose 覆盖文件。

## 6. 验收命令

```bash
# 单元测试
cd backend
PYTHONPATH=..:. pytest tests/unit -q

# 真实队列、真实模型、mock 企微出站
sh scripts/run_conversation_smoke_compose.sh

# 单次突发与同用户顺序
LOAD_USERS=60 sh scripts/run_conversation_load_compose.sh

# 固定并发持续负载；正式验收 duration=14400（4 小时）
LOAD_CONCURRENCY=<2x_C_peak> \
LOAD_DURATION_SECONDS=14400 \
sh scripts/run_conversation_sustained_compose.sh

# Redis/MySQL 短断
CHAOS_USERS=20 sh scripts/run_conversation_chaos_compose.sh

# 20% LLM timeout/429/500/坏 JSON
CHAOS_USERS=20 sh scripts/run_conversation_llm_chaos_compose.sh

# 人工标注、合成或匿名历史回放
EVAL_MODE=curated EVAL_REPEAT=5 \
  sh scripts/run_conversation_replay_compose.sh
EVAL_MODE=historical EVAL_LIMIT=500 EVAL_REPEAT=5 \
  sh scripts/run_conversation_replay_compose.sh
```

混沌脚本会暂停隔离环境中的 Redis/MySQL 或重建 Worker，不得指向生产容器。

## 7. 已有验证证据

- 单元测试：1281 条通过。
- 真实服务烟测：13/13。
- 真实模型合成广度集：599/600；唯一失败修复后连续 5/5。
- 36 条人工标注集重复 3 次：semantic/stable 100%。
- Redis 暂停 4 秒：20/20 恢复，9.904 秒收敛。
- MySQL 暂停 7 秒：20/20 恢复，15.437 秒收敛。
- 20% LLM 混合故障：20/20 完成，四类故障均命中。
- 32 Worker / 60 同时用户短突发：60/60，端到端 p95 4.022 秒，
  queue p95 1.976 秒，同用户 3/3 有序。

这些数据证明预生产和受控灰度条件，不替代真实 `C_peak` 长稳态、匿名历史语料
和生产观察周期。

## 8. 回滚

数据库变更以新增表、列、索引和兼容枚举为主，回滚应用时建议保留 schema，
不要在故障窗口执行破坏性 down migration。

1. 停止入口放量和新消息接入。
2. 保持新 Worker 运行，等待以下查询归零：

```sql
SELECT status, COUNT(*) FROM wecom_inbound_event
WHERE status IN ('received','processing','failed','session_pending')
GROUP BY status;

SELECT status, COUNT(*) FROM wecom_outbound_outbox
WHERE status IN ('pending','sending')
GROUP BY status;
```

3. 若不能归零，先修复依赖或人工处理，不要直接切回旧 Worker；旧 Worker 不认识
   `session_pending`，也不会消费 outbox。
4. 停止新 Worker，部署基线 app/worker。
5. 验证 `/health` 和基础文本消息。
6. 保留新增 schema 供审计和后续 forward-fix；确认不再回滚后再安排独立清理。

## 9. 已知边界

- 企微 `message/send` 没有客户端幂等键。“企微已接收但 HTTP 响应丢失”窗口仍可能
  产生重复回复；系统保证业务路由不重跑、回复意图不丢，但不能宣称严格
  end-to-end exactly-once。
- 32 Worker / 60 用户只是当前机器与模型延迟下的一次短突发结果，不能直接作为
  生产固定容量。
- 正式全量前仍需业务提供 `C_peak`、至少 500 条匿名历史人工标注语料，并完成
  shadow、分级灰度和全量后观察期。
