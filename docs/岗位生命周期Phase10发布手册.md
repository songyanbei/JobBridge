# 岗位生命周期 Phase 10 发布手册

## Session schema 兼容性门禁

`phase10_003_session_commit_deadline.sql` 和
`phase10_004_session_commit_lease_owner.sql` 改变了 durable session commit 的运行时契约。
迁移期间禁止新旧 API 或 Worker 混跑。

执行约束：

1. 停止所有 API、消息 Worker 和 session recovery Worker，并确认旧进程已退出。
2. 依次执行 003、004 迁移；不得在旧 Worker 仍可写入时执行回填。
3. 运行 Phase 10 preflight，确认 deadline 为 `DECIMAL(20,6) NULL`、lease owner 为
   `VARCHAR(64) NULL`，并确认 `idx_session_commit_due` 列序完全匹配。
4. 确认实际 Redis 为 `noeviction`、AOF 已开启且 `appendfsync=always`。
5. 只有 preflight 返回 `ready=true` 后，才允许部署同一版本的新 API 和 Worker。

禁止滚动发布跨越 003/004 schema 边界。旧 Worker 不写 deadline 或 lease owner，会绕过
新版本的绝对截止和 owner fencing；发现任何旧实例仍存活时，发布必须停止。

完整迁移、回填、开关、监控和回滚顺序将在后续发布门禁章节补全。
