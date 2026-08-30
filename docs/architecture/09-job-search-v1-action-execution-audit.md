# Job Search v1 Action Execution Audit

审计基线：`2026-08-30`，分支 `codex/unified-listing-flow-architecture`。

验证状态：本轮 WSL 部署和模拟对话已完成；实际日志确认搜索请求仍为 `execution_mode=off` / `served_assignment=legacy`，`action_execution` 近期无生产搜索记录。该结果与本审计结论一致，不能将通用 Action 契约测试等同于生产链路已接入。

## 结论

当前生产搜索链路**没有真正调用** `action_execution_service.py`，因此不能宣称搜索 read action 已记录
`turn_id + action_name` 执行事实。基于当前事务边界，本轮不做半接入，也不改变搜索、重试或出站行为。

## 生产调用链证据

- `webhook.py` 为每个入站事件生成 `turn_id`，并把它写入队列；`worker.py` 的
  `_build_wecom_message` 和恢复队列也会透传该字段。这说明幂等键的输入已经具备。
- `message_router.py` 的 `_run_search`、`_handle_show_more` 和确认放宽路径会调用
  `JobSearchFacade` 或 legacy `search_service`，但没有 claim/read/finalize 调用。
- `listing/search.py` 只是旧搜索服务的 adapter、Card 投影和快照操作，也没有 action lease。
- `rg` 全量检查 `backend/app` 后，`action_execution_service` 只被自身定义引用，没有 worker、router 或
  listing facade 的生产 import/call site。

## 事务边界

`Worker._process_locked` 的顺序是：

1. 将 inbound event 标为 `processing` 并单独 `db.commit()`；
2. 调 router，router 通过 `conversation_service` 在 ContextVar 中暂存 Session mutation；
3. 在同一个后续 DB 事务中写 ConversationLog、Outbox、推荐事实和 Session commit payload，并标记 event
   完成；
4. DB commit 成功后，才调用 `apply_staged_session` 对 Redis 做 version/CAS，失败时进入
   `session_pending` 恢复。

因此 Outbox/业务事实可以和 action finalize 放进同一个 DB 事务，但真实 Session CAS 不在这个事务里。
此外，`ActionExecution` 只有 `result_digest`，没有 `snapshot_id`、结果 payload 或可重建回复的引用；成功
重试无法仅凭该行复用首轮搜索回复。若先提交 claim 再执行慢搜索，还会把 router 之前的未提交写入一并提交；若只接
finalize，则会产生没有可安全 replay 的“成功事实”。

## Blocker 与后续入口

要满足架构 4.2，至少需要先确定一项正式设计：

- 为 action execution 保存可定位的结果/快照引用，并让 Worker 在成功 replay 时只补投已有 Outbox；
- 或把 Session 的可审计 CAS 纳入同一可提交事务（而不是当前 DB + Redis 两阶段恢复模型）。

在这两项完成前，不能把 claim/replay 包在 listing facade 外层，也不能把 finalize 插入 Worker 的 DB commit
而声称满足 4.2。本审计新增的契约测试会锁定“当前未接入”和“Session CAS 晚于 DB commit”这两个事实，未来接入时
应先更新契约和 replay 设计，再删除这些 blocker 断言。
