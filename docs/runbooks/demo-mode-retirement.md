# 演示模式下架与移除操作手册

> 适用日期：2026-09-03；仅适用于 development/test 的演示工作区。

演示模式是独立控制面，不新增业务 `super_admin`，也不修改真实用户的
`User.role`、企微身份绑定、legacy Webhook 或共享 Redis 队列。

## 生命周期

```text
active -> disabled -> cleaning -> cleaned
                         \-> failed -> retry
```

`disabled`、`cleaning`、`failed`、`cleaned` 都不能恢复为 `active`；需要重新演示时创建新的 `demo_id`。

## 标准下架步骤

1. 由 `super_admin` 调用 `POST /admin/demo/{demo_id}/disable`，填写原因和可选的 `expected_version`。
2. 调用 `GET /admin/demo/{demo_id}/preview`，核对资源类型和数量；发现真实用户、真实岗位/简历或非演示 actor 时立即停止。
3. 确认没有新的演示流量后，调用 `POST /admin/demo/{demo_id}/cleanup`。
4. 如果返回失败，先查看 workspace 状态和错误原因，再调用 `POST /admin/demo/{demo_id}/cleanup/retry`；不要手工删除数据库行。
5. 只有状态为 `cleaned`，且资源状态全部为 `cleaned`、无 active member、无 pending/sending 的 AIBot outbox 后，才允许人工评审是否执行 Phase 17 down migration。

## 安全边界

- `scripts/demo_env_cleanup.sh` 已停用，会拒绝执行，不会连接 MySQL 或 Redis。
- 禁止按 `demo_*`、`test_e2e_*` 等前缀删除；这些前缀不是可靠的 workspace 所有权证明。
- 禁止执行共享队列的 `DEL`；演示清理只删除 `demo:session:{demo_id}:*` 和明确的演示 active pointer。
- Phase 17 down migration 会先检查 workspace/resource/member、AIBot outbox 和 inbound in-flight 状态；检查不通过时由数据库 `SIGNAL` 阻断，不会 drop 控制面表。
- 前端独立演示工作台尚未作为本轮阻断项实现；可先使用受保护的 `/admin/demo/*` API，页面接入必须复用这些 API，不得绕过控制面。

## 恢复与审计

每次操作应保存：脱敏的 `demo_id`、操作者、版本号、preview 计数、cleanup 结果、失败原因和提交版本。不要记录 Bot Secret、企业 Secret、Token、EncodingAESKey、完整 actor 或联系方式。
