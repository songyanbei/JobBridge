# Job Search v1 Action Execution Audit

审计基线：`2026-08-30`，分支 `codex/unified-listing-flow-architecture`。

## 复核状态

原审计识别的“Action 只有 schema、尚未进入搜索调用链、无法安全 replay”阻塞项已在 S3 实施中关闭。当前 Worker 已接入 ActionGateway、parse artifact、claim/fencing、finalize、结果引用和 replay；Router/Facade 只消费 `ActionContext`，不自行 claim/finalize。

当前生产仍保持 `action_execution_mode=off`、`served_assignment=legacy` 优先，因此生产日志中 Action on 记录为空是预期发布边界，不是代码未接入。WSL 和 mock testbed 已完成实际验证；Action on 灰度、7 天观察窗口及 legacy 退出审批尚未完成。

## 结论

1. `search_job`、`show_more_job`、`relax_job` 已具备统一的 `turn_id + action_name` 幂等键、lease/fencing 和结果引用。
2. Worker 在 Router 前完成单次 Gateway parse；`acquired` 才执行路由，`succeeded`/`in_progress`/`failed_terminal` 分别进入 replay、等待或终态路径，避免重复搜索和重复副作用。
3. 成功 Action 绑定 `request_id`、`snapshot_id`、delivery/outbox 和 Session commit 引用；replay 只补投 Outbox 或恢复 Session CAS，不重新调用 provider、SQL/rerank 或 relaxation probe。
4. DB commit 与 Redis Session CAS 仍是两阶段边界：DB 成功而 CAS 失败进入 `session_pending`，由 reconciler 恢复；这不是伪装成单一 ACID 事务的设计。

## 生产调用链证据

- `backend/app/services/worker.py` 已在处理锁内调用 `ActionGateway`，按 rollout/mode 决定 off、shadow、claim、busy、replay 或 acquired 分支，并把 `action_context` 传入 Router。
- `backend/app/services/message_router.py` 的搜索、`show_more` 和放宽路径接收 `ActionContext`；Router/Facade 不直接 claim/finalize，最终结果 metadata 回传 Worker。
- `backend/app/services/intent_service.py` 提供 Gateway 与 Router 共用的单次解析适配器；`parse_ref`、digest、schema/session version 绑定在 Action 上，避免一轮消息二次调用 LLM。
- `ActionExecution` 已具备结果引用相关字段和 replay 索引；`ActionParseArtifact` 保存 PII-free parse，覆盖 Redis cache miss 和跨 Worker 重试。

## 事务与恢复边界

`Worker._process_locked` 的 Action 路径顺序为：

1. 持久化并 claim Action lease（独立短事务）；
2. 传递同一 parse/action context 调用 Router，暂存 Session mutation 和业务结果；
3. 在结果事务中写 ConversationLog、推荐事实、Outbox、durable Session commit、结果引用并 finalize Action；
4. DB commit 成功后执行 Redis Session CAS；失败时标记 `session_pending`，reconciler 只恢复 CAS/Outbox，不重跑 Router。

Fencing 条件同时约束 claim、finalize、replay 状态更新；旧 Worker 不能越过新 lease 写成功事实。结果引用缺失、digest 冲突、snapshot 过期或 actor 不匹配均 fail-closed，不通过重新搜索来“修复”。

## 验证记录

- S3 核心集合：`201 passed`。
- S2 搜索/翻页/Replay/权限集合：`216 passed`。
- Worker 定向回归：`72 passed`。
- Action preflight：通过；C2 故障矩阵：`9/9 passed`。
- WSL 一键 smoke/full-smoke：通过；mock 对话覆盖初始搜索、多轮补充、`更多`、放宽、`/帮助`、重复 MsgId、身份守卫、Redis outbound/SSE。

## 剩余门禁

- 默认配置仍为 `action_execution_mode=off`，需按 1% -> 5% -> 25% -> 50% -> 100% 进行生产 on 灰度。
- A4/B4/C3 要求的连续观察窗口、重复 provider/Outbox 为 0、PII 泄露为 0 和 legacy 退出签字尚未完成。
- 在上述门禁完成前，不得启动 S4 岗位发布，也不得删除 legacy fallback 或历史明文列；清理必须另行审批并保留可审计回滚路径。
