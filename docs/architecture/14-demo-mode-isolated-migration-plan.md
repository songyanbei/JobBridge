# 演示模式独立隔离改造方案

> 版本：v1.0  
> 编写日期：2026-09-03（Asia/Shanghai）  
> 适用仓库：JobBridge 企微智能机器人及管理后台  
> 状态：调研结论与实施基线；本文件落地后再进入代码实施评审

## 1. 目标与结论

本方案解决以下问题：

1. 同一个企业微信智能机器人账号、同一个企微用户，可以体验求职者、厂家、中介三种业务角色；
2. 不改变现有 `worker / factory / broker` 业务角色模型；
3. 不把后台 `super_admin` 混入业务用户角色；
4. 演示数据与真实业务数据隔离；
5. 演示模式可以独立禁用、下架、清理和重建；
6. 生产环境默认关闭，且无法通过普通配置误开启。

最终采用：

```text
同一企微账号作为真实入口身份
        +
会话级演示角色切换
        +
三个独立 synthetic demo principals
        +
demo_id 全链路隔离
        +
独立禁用/清理控制面
```

不采用以下方案：

- 不新增业务 `super_admin` 或 `all_roles` 角色；
- 不直接修改真实 `User.role` 来模拟角色切换；
- 不把三个角色的状态塞进同一个 `session:{userid}`；
- 不使用 `/删除我的信息` 承担演示清理；
- 不通过关闭外键检查或按用户名前缀盲删数据。

## 2. 当前代码基线

### 2.1 业务角色与身份

当前 `User.role` 仅支持：

```text
worker / factory / broker
```

未知用户通过 `identify_or_register()` 默认自动注册为 `worker`。企微 AIBot 路径先经过：

```text
opaque actor
  -> wecom_aibot_identity
  -> aibot_identity_binding
  -> canonical_userid
  -> user
  -> UserContext
```

关键代码：

- `backend/app/models.py`
- `backend/app/services/aibot_identity_service.py`
- `backend/app/services/registration_service.py`
- `backend/app/services/user_service.py`
- `backend/app/services/worker.py`

后台 `AdminUser.role` 的 `viewer / operator / super_admin` 属于后台控制面，不属于业务身份。

### 2.2 角色判断分布

`UserContext.role` 或 `SessionState.role` 被消息路由、命令、Dialogue、搜索权限、可见性、上传、Contact、推荐和岗位/简历生命周期共同使用。角色切换必须在业务上下文边界实现，不能只修改 Prompt 或单个路由分支。

### 2.3 会话与队列

当前单聊会话最终使用：

```text
session:{canonical_userid}
```

`SessionState` 保存搜索条件、候选快照、分页、上传草稿、放宽确认、pending action、历史和版本号，并由 Redis CAS 管理。

AIBot 入站链路已经独立为：

```text
AIBot WSS -> durable inbound event -> queue:incoming -> Worker
          -> message_router -> outbox -> AIBot connector
```

演示模式应复用该可靠链路，不在 WebSocket Reader 中加入角色或业务逻辑。

### 2.4 现有演示资产

当前已有独立 `mock-testbed`，支持三个独立模拟用户，并复用真实 Worker、搜索、推荐和数据库链路：

- `mock-testbed/README.md`
- `mock-testbed/backend/routes.py`
- `mock-testbed/frontend/src/views/MockSplitView.vue`
- `mock-testbed/sql/seed_mock_users.sql`

该测试台继续保留，作为离线回归和开发演示工具。本方案新增的是“真实企微账号切换三个角色”的控制面，不替换 mock-testbed。

## 3. 方案设计

### 3.1 真实入口身份与演示业务主体分离

真实企微身份只负责身份验证、接收消息、接收最终回复和审计中的真实操作者；演示业务主体负责角色权限、岗位/简历 owner、搜索数据范围、Contact/推荐关联和演示清理。

示例：

```text
真实企微用户：wecom-test-user
真实角色：worker（保持不变）

演示批次：demo-20260903-xxxx
  - demo_worker_<id>   role=worker
  - demo_factory_<id>  role=factory
  - demo_broker_<id>   role=broker
```

### 3.2 DemoActorContext

建议新增独立上下文，不破坏普通路径：

```python
@dataclass(frozen=True)
class DemoActorContext:
    demo_mode: bool
    demo_id: str
    real_actor_userid: str
    effective_userid: str
    active_role: str
    bot_id: str
    workspace_status: str
```

必须明确区分：

```text
real_actor_userid
  用于企微回复、真实身份、审计和安全校验

effective_userid
  用于演示业务查询、岗位/简历 owner、权限和清理
```

当前 `_reply()` 使用 `user_ctx.external_userid` 作为回复对象。实施时必须增加 reply/业务主体的明确分离，不能把 synthetic userid 直接传给企业微信。

### 3.3 角色切换命令

只允许确定性命令：

```text
/演示
/演示 求职者
/演示 厂家
/演示 中介
/退出演示
```

不允许由 LLM 根据自然语言自动推断角色切换。workspace 非 `active` 时，所有演示命令只返回确定性拒绝。

### 3.4 开关与 allowlist

新增配置建议：

```dotenv
DEMO_MODE_ENABLED=false
DEMO_ALLOWED_BOT_IDS=
DEMO_ALLOWED_ACTOR_DIGESTS=
DEMO_SESSION_TTL_SECONDS=1800
DEMO_MAX_ACTIVE_WORKSPACES=1
```

启用条件必须同时满足：

```text
APP_ENV in {development, test}
AND DEMO_MODE_ENABLED=true
AND bot_id 在 allowlist
AND actor digest 在 allowlist 或 workspace membership 中
AND workspace.status=active
AND conversation_type=single
```

生产环境启动时强制关闭演示模式；生产环境不识别演示指令；群聊默认拒绝进入演示模式。

## 4. 数据模型与隔离

### 4.1 控制面模型

新增：

```text
demo_workspace
- demo_id
- name
- status: active / disabled / cleaning / cleaned / failed
- bot_id
- opaque_actor_digest
- canonical_actor_userid
- created_by / created_at
- disabled_at / cleaned_at
- version / reason

demo_principal
- demo_id
- role
- synthetic_userid
- status
- created_at

demo_resource
- demo_id
- resource_type
- resource_id
- lifecycle_status
- created_at / cleaned_at
```

约束：一个 workspace 必须恰好有 worker/factory/broker 三个 principal；synthetic userid 禁止使用真实企微 userid；清理完成后不可重新激活，需新建批次。

### 4.2 资源标记策略

长期目标是为核心资源增加显式 `demo_id` 字段和索引，至少覆盖：

- `user`、`job`、`resume`；
- `conversation_log`、`audit_log`、`event_log`；
- `wecom_inbound_event`、`wecom_outbound_outbox`；
- `action_execution`、`action_parse_artifact`；
- `contact_request`、`contact_grant`、`contact_delivery`、`contact_access_audit`；
- `recommendation_request`、`recommendation_search_attempt`、`recommendation_delivery`、`recommendation_impression`；
- `job_replacement`、`resume_replacement`、`domain_outbox_event`、`target_cleanup_task`。

过渡阶段可以使用 `demo_resource` 做资源清单，但每一个演示资源创建必须登记，不能依赖 userid 前缀猜测。

所有演示查询必须通过统一 scope helper，禁止在单个业务模块中自行拼接演示条件。缺少演示 scope 的查询必须 fail-closed。

### 4.3 数据可见性

演示模式下：

- worker 只能搜索当前 workspace 的演示岗位；
- factory 只能搜索当前 workspace 的演示简历；
- broker 只能在当前 workspace 内双向搜索；
- 不展示真实岗位、真实简历和真实联系方式；
- 不允许演示 Contact 触达真实用户；
- 推荐、报表和统计默认按 `demo_id` 隔离。

## 5. Redis 会话设计

角色指针：

```text
demo:active:{real_actor_userid}
```

角色专属 session：

```text
demo:session:{demo_id}:single:{real_actor_userid}:worker
demo:session:{demo_id}:single:{real_actor_userid}:factory
demo:session:{demo_id}:single:{real_actor_userid}:broker
```

角色切换只改变 active pointer，不修改真实用户角色，也不覆盖其他角色 session。三种角色不共享搜索条件、候选快照、上传草稿、放宽确认和 pending action。

## 6. 消息链路改造边界

```text
AIBot callback
  -> identity resolve
  -> demo workspace resolve
  -> DemoActorContext
  -> effective business principal
  -> existing Worker/Router/Search/Upload/Contact
  -> reply to real_actor_userid
```

必须遵守：

1. WebSocket Reader 不负责角色切换和业务执行；
2. Worker 在身份验证后构造 DemoActorContext；
3. 业务写入使用 `effective_userid`；
4. 企业微信 Outbox 回复使用 `real_actor_userid`；
5. 入站、出站、审计和业务日志带 `demo_id`、`active_role` 和 actor digest；
6. workspace 被禁用后，已排队演示消息不能继续产生业务副作用；
7. AIBot identity binding、真实 User 和真实 session 不参与演示清理。

## 7. 禁用、下架与清理

### 7.1 禁用/下架

```text
active -> disabled
  -> 拒绝新的演示命令和业务动作
  -> 停止演示 Outbox 自动发送
  -> 演示岗位/简历走现有下架流程
  -> 撤销或过期演示 Contact/Grant
```

禁用必须幂等，且不能影响真实用户、真实 AIBot binding、legacy Webhook 或生产数据。

### 7.2 清理顺序

```text
1. workspace -> cleaning
2. 阻止新的演示业务执行
3. 冻结/终止演示 Action、Contact、Outbox
4. 岗位和简历软下架
5. 处理媒体与 target cleanup
6. 清理推荐正文、delivery、impression、attempt、request
7. 清理 Contact request/grant/delivery/audit
8. 清理 action、parse artifact、domain outbox
9. 清理 inbound event、conversation log、event log
10. 清理演示 audit 明细，仅保留 workspace 清理摘要
11. 清理演示 Redis session、active pointer、索引和临时 key
12. 删除 demo principals 及其 user 行
13. workspace -> cleaned
```

实际删除顺序必须服从现有外键和推荐隐私清理顺序。禁止关闭外键检查，禁止复用真实用户删除流程。

清理前必须预览数量；清理过程必须分批、幂等、可重试、有进度。失败时 workspace 标记为 `failed`，从 checkpoint 重试，不自动恢复为 `active`。

## 8. 管理后台

新增独立演示工作台，不塞入普通清理任务页面。

```text
GET  /admin/demo/workspaces
GET  /admin/demo/{demo_id}
GET  /admin/demo/{demo_id}/preview
POST /admin/demo/{demo_id}/disable
POST /admin/demo/{demo_id}/cleanup
GET  /admin/demo/{demo_id}/cleanup-status
POST /admin/demo/{demo_id}/cleanup/retry
```

权限建议：查询允许 `operator/super_admin`；创建、禁用、清理只允许 `super_admin`；清理建议二次确认或四眼审批。后台展示 workspace 状态、脱敏 actor、三个 principal、资源预览、清理进度和失败原因。

## 9. 实施拆分

### P0：控制面与契约

- 新增 workspace/principal/resource 迁移设计；
- 确认 DemoActorContext 及 reply/effective userid 契约；
- 完成生产 fail-closed 校验；
- 固化清理顺序和回滚策略。

### P1：会话和角色切换

- 新增 active pointer 和三角色 session namespace；
- 实现 `/演示`、`/退出演示`；
- 实现角色切换时的状态隔离；
- 普通路径回归保持不变。

### P2：业务上下文与数据 scope

- Worker/Router 接入 DemoActorContext；
- Search/Upload/Contact/Recommendation 使用有效业务主体；
- Outbox 回复目标保持真实 actor；
- 资源统一登记并限制查询范围。

### P3：后台清理与下架

- 新增 demo 管理 API 和前端工作台；
- 新增预览、禁用、清理、重试和进度；
- 接入 super_admin 与四眼审批；
- 完善 Redis、媒体、推荐、Contact、Action 清理。

### P4：企微真实联调

- 真实 AIBot 单聊角色切换；
- 三角色完整业务路径；
- 禁用后消息阻断；
- 清理后批次不可恢复；
- WSS 重连、Worker 重启、旧消息重放回归。

## 10. 验收门禁

- 同一企微 actor 可切换三个角色，真实 `User.role` 始终不变；
- 三个角色 session、搜索条件、候选快照、草稿完全隔离；
- 演示岗位和简历 owner 是 synthetic principal；
- 演示查询不返回真实数据和联系方式；
- 演示 Contact 不触达真实用户；
- 禁用后不再产生新的演示业务副作用；
- 清理前可预览，清理可重试、幂等、有进度；
- 清理后无孤立推荐、Contact、Action、Outbox、媒体和 Redis key；
- 非开发环境、非 allowlist actor、群聊入口均 fail-closed；
- Worker 重启、AIBot 重连和旧消息重放不会恢复到错误角色；
- legacy 业务和现有全量回归保持原基线。

## 11. 最终决策

```text
真实企微身份：只做入口和回复
真实 User.role：保持原值
演示角色：由 workspace active pointer 决定
演示业务主体：三个独立 User 行
演示会话：三个独立 Redis namespace
演示数据：demo_id 全链路登记/过滤
演示清理：独立服务和后台工作台
```

该模型能满足同一企微账号体验三个角色，同时保持现有业务权限、生命周期、AIBot 长连接和 legacy Webhook 体系不被破坏，并支持演示批次独立下架、清理和重建。

