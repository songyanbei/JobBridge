# Mock 企业微信测试台 开发 Checklist

> 基于：`collaboration/features/phase7-mock-wework-testbed.md`
> 配套实施文档：`collaboration/features/phase7-mock-wework-testbed-dev-implementation.md`
> 面向角色：后端 + 前端开发
> 状态：`draft`
> 创建日期：2026-04-19

## A. 开发前确认

- [ ] 已阅读 `collaboration/features/phase7-mock-wework-testbed.md`（主文档）
- [ ] 已阅读 `collaboration/features/phase7-mock-wework-testbed-dev-implementation.md`
- [ ] 已粗读企业微信官方 OAuth2 / message/send / 回调加解密章节（字段来源）
- [ ] 已确认 Phase 0~7 已完成，未闭环项不阻塞本任务
- [ ] 已确认**不改动** `api/webhook.py` 真实路径 / `services/*` / `models.py` / `admin/*` / `message_router.py` / Worker 中的任何业务逻辑
- [ ] 已确认**只允许两处切口**：新建 `api/mock_wework.py` + `wecom/client.py` 加 flag 分支
- [ ] 已确认所有字段名对齐企业微信官方契约（不翻译、不改大小写、不改结构）
- [ ] 已确认模拟层走 `external_userid`，**不得出现 `userid`** 字段
- [ ] 已确认 `MOCK_WEWORK` 默认 `false`，仅本地 / 预发可开启
- [ ] 已确认前端 `/mock/*` 跳过 `authStore` 守卫，**不颁发 JWT**

## B. 配置与 Flag

- [ ] `backend/app/config.py` 新增 `mock_wework: bool = Field(default=False, alias="MOCK_WEWORK")`
- [ ] `backend/app/config.py` 新增 `@model_validator(mode="after")` 校验 `env=="production" and mock_wework==True` 抛 `RuntimeError`
- [ ] 本地执行 `ENV=production MOCK_WEWORK=true python -c "from app.config import settings"` 立即报错
- [ ] `ENV=production MOCK_WEWORK=false` 正常加载
- [ ] `ENV=development MOCK_WEWORK=true` 正常加载
- [ ] `.env.example` 追加 `MOCK_WEWORK=false` + 注释
- [ ] 所有 `if settings.mock_wework:` 分支都打了 `logger.warning` 留痕

## C. 数据库 Seed

- [ ] `backend/sql/seed_mock_users.sql` 已创建
- [ ] 使用 `INSERT ... ON DUPLICATE KEY UPDATE` 保证幂等
- [ ] seed 至少覆盖 `worker` × 2 / `factory` × 1 / `broker` × 1
- [ ] 所有 `external_userid` 以 `wm_mock_` 前缀开头
- [ ] 文档注释包含清理 SQL：`DELETE FROM user WHERE external_userid LIKE 'wm_mock_%';`
- [ ] seed 后 `SELECT COUNT(*) FROM user WHERE external_userid LIKE 'wm_mock_%'` ≥ 4

## D. 出站总线

- [ ] 新建 `backend/app/services/mock_outbound_bus.py`
- [ ] `publish(target_key, payload)` 实现，channel 格式 `mock:outbound:{target_key}`
- [ ] `subscribe(target_key)` 返回 Redis pubsub 对象
- [ ] `iter_frames(pubsub)` 输出 SSE 帧（`event: message` / `event: ping`）
- [ ] `iter_frames` 每 15s 自动发 `event: ping`
- [ ] `unsubscribe(pubsub)` 正确清理、异常吞掉不抛出
- [ ] 点对点消息 target_key = `external_userid`
- [ ] 群消息 target_key = `chat:{chat_id}`

## E. 出站拦截

- [ ] `backend/app/wecom/client.py` `send_text` 方法体**第一行**加 `if settings.mock_wework:` 分支
- [ ] `send_text_to_group` 同样位置加分支
- [ ] 分支内调用 `mock_outbound_bus.publish` 并 return
- [ ] 真实调用路径 `self._http_post(...)` **diff 行数 = 0**（用 `git diff` 确认）
- [ ] mock 分支返回 `{errcode:0, errmsg:"ok", msgid:"mock_..."}`
- [ ] `MOCK_WEWORK=false` 下真实路径完全不受影响（单测断言 `_http_post` 被调用）
- [ ] `MOCK_WEWORK=true` 下 `_http_post` **绝不**被调用（单测断言 0 次）

## F. 后端 Mock 路由

- [ ] 新建 `backend/app/api/mock_wework.py`
- [ ] router 前缀 `/mock/wework`，tag `mock-wework`

### F.1 GET /users
- [ ] SQL 只查 `wm_mock_%` 前缀
- [ ] 响应字段集合 `{errcode, errmsg, users}`
- [ ] `users[]` 每项含 `{external_userid, name, role, avatar}`

### F.2 GET /oauth2/authorize
- [ ] 返回 302
- [ ] Location 含 `code=MOCK_CODE_<hex>` 和（若提供）`state=<原值>`
- [ ] 不校验 appid / agentid

### F.3 GET /code2userinfo
- [ ] 响应 `{errcode:0, errmsg:"ok", external_userid, openid}`
- [ ] 字段名严格小写 + 下划线，无驼峰
- [ ] 支持 `x_mock_external_userid` query 覆盖身份

### F.4 POST /inbound
- [ ] 请求体必填字段 `{ToUserName, FromUserName, CreateTime, MsgType, Content, MsgId, AgentID}`
- [ ] 缺字段返回 `{errcode:40001, errmsg:"missing fields: [...]"}`
- [ ] Redis L1 幂等：`SET wecom:msg:{msg_id} NX EX 600`
- [ ] DB L2 幂等：查 `wecom_inbound_event`
- [ ] 幂等命中返回 `errcode:0 errmsg:"ok (duplicate ...)"`，不报错
- [ ] 构造 `WeComMessage` 字段与 `webhook.py` 解密后一致
- [ ] 写 `wecom_inbound_event` 字段与 `webhook.py` 一致
- [ ] `rpush queue:incoming` payload 字段与 `webhook.py` 一致（snapshot 测试覆盖）

### F.5 GET /sse
- [ ] `media_type: text/event-stream`
- [ ] header 含 `X-Accel-Buffering: no`
- [ ] header 含 `Cache-Control: no-cache`
- [ ] 先发 `event: ready` 帧
- [ ] 订阅 `mock:outbound:{external_userid}`
- [ ] 每 15s 自动发 `event: ping`
- [ ] 客户端断开时正确释放 pubsub

## G. 条件注册

- [ ] `backend/app/main.py` 末尾（或路由注册段）新增 `if settings.mock_wework: app.include_router(mock_wework.router)`
- [ ] 同时打印 `logger.warning("MOCK_WEWORK enabled — ...")`
- [ ] `MOCK_WEWORK=false` 下访问 `/mock/wework/users` → 404
- [ ] `/admin/*`、`/webhook/*`、`/api/events/*` 路由不受影响

## H. 前端路由

- [ ] `frontend/src/router/index.js` 新增 `/mock`、`/mock/split`、`/mock/single` 三条路由
- [ ] 三条路由均 `meta: { skipAuth: true }`
- [ ] `router.beforeEach` 开头插入 `if (to.meta?.skipAuth) return true`
- [ ] `/mock` 路径访问不触发跳转 `/admin/login`
- [ ] 进入 `/mock` 后 `authStore.token` 保持原值（不污染）

## I. 前端组件

### I.1 MockBanner.vue
- [ ] 红色（#d32f2f）横幅，fixed top，z-index 9999
- [ ] 文案含 `MOCK-WEWORK TESTBED` 和「非真实企业微信环境」字样
- [ ] **无 props**、**无 v-if 开关**
- [ ] 任何 URL 参数都无法隐藏

### I.2 MockIdentityPicker.vue
- [ ] 挂载时调 `fetchMockUsers()`
- [ ] 支持 `roleFilter` prop（数组）
- [ ] 按 role 分组渲染，label 中文（求职者 / 招聘者（厂家）/ 招聘者（中介））
- [ ] 支持 `filterable` 搜索

### I.3 MockChatPanel.vue
- [ ] 身份切换时 `es.close()` 旧连接 + 清空 `messages`
- [ ] 发送消息 payload 字段严格 `{ToUserName, FromUserName, CreateTime, MsgType, Content, MsgId, AgentID}` 大小写
- [ ] 气泡区分 `direction=in / out` 样式
- [ ] 回车触发发送
- [ ] `onBeforeUnmount` 关闭 SSE

### I.4 MockSplitView.vue
- [ ] 顶部挂 `MockBanner`
- [ ] 左栏 `roleFilter=['factory', 'broker']`
- [ ] 右栏 `roleFilter=['worker']`
- [ ] 两栏 SSE 相互独立

### I.5 MockSingleView.vue
- [ ] 从 URL query 读 `external_userid` 和 `role`
- [ ] 根据 role 选择 roleFilter
- [ ] 支持刷新后 URL 状态保留

### I.6 MockEntryView.vue
- [ ] 根据 URL 是否带 `external_userid` 动态选 Single / Split

## J. 前端 API 层

- [ ] `frontend/src/api/mock.js` 已创建
- [ ] `fetchMockUsers()` GET `/mock/wework/users`
- [ ] `mockInbound(payload)` POST `/mock/wework/inbound`
- [ ] `openMockSse(id, {onMessage})` 返回 `EventSource`
- [ ] EventSource 监听 `message` / `ping` / `ready` / `error` 四种事件
- [ ] 关闭 SSE 用 `es.close()`

## K. 字段契约自查（开发自测必跑）

- [ ] `/mock/wework/code2userinfo` 响应字段集合 `== {errcode, errmsg, external_userid, openid}`
- [ ] `/mock/wework/inbound` 请求字段大小写**完全等于**企微 XML 标签名
- [ ] SSE `event: message` 的 `data` JSON 字段 `== {touser, msgtype, agentid, text}`
- [ ] `touser`、`toparty`、`totag` 在消息 payload 中为 `str` 类型
- [ ] `grep -rn "\"userid\"" backend/app/api/mock_wework.py` 结果 = 0
- [ ] `grep -rn "\"userid\"" frontend/src/components/mock/` 结果 = 0
- [ ] 返回体顶层恒有 `errcode` 和 `errmsg`

## L. 本地联调冒烟

- [ ] `.env` 设 `MOCK_WEWORK=true` 和 `ENV=development`
- [ ] `docker compose up -d` 5 容器 healthy
- [ ] 后端 warning 日志含 `MOCK_WEWORK enabled`
- [ ] 浏览器访问 `/mock` 显示红色横幅 + 双栏
- [ ] 身份切换器下拉能列 seed 的 `wm_mock_*` 用户
- [ ] 任一栏发消息 → `conversation_log` 新增 `direction=in` 行
- [ ] Worker 日志显示消费 `queue:incoming`
- [ ] 2s 内 SSE 气泡弹出 bot 回复
- [ ] 多窗口：`/mock/single?external_userid=wm_mock_worker_001&role=worker` 进入单视角
- [ ] 刷新后 URL 参数仍在，身份保留
- [ ] `MOCK_WEWORK=false` 重启 → `/mock` 返回 404，`/admin/*` 仍正常

## M. 单元测试（详见 `phase7-mock-wework-testbed-test-implementation.md`）

- [ ] Flag 守卫单测（TC-MW-1.*）已写并通过
- [ ] 出站拦截单测（TC-MW-4.*）已写并通过
- [ ] `/mock/wework/*` 路由字段契约单测（TC-MW-2.* / TC-MW-3.*）已写并通过
- [ ] 入站桥接与 `webhook.py` 字段一致性单测（TC-MW-3.5 / 3.6）已写
- [ ] SSE 单测（TC-MW-5.*）已写并通过
- [ ] `pytest -q backend/tests/unit/test_mock_wework*.py` 全绿

## N. 提交前全局检查

- [ ] `git diff main..HEAD -- backend/app/` 中业务文件 diff 行数 = 0
  - 业务文件定义：`api/webhook.py` / `api/events.py` / `api/admin/*` / `services/*`（除新建 `mock_outbound_bus.py`）/ `models.py` / `message_router.py` / Worker 相关
- [ ] `git diff main..HEAD -- backend/app/wecom/client.py` 仅见两段 `if settings.mock_wework:` 新增
- [ ] `ruff check backend/` 无错误
- [ ] `pytest backend/tests/` 全绿（既有测试不被破坏）
- [ ] `cd frontend && npm run build` 成功
- [ ] 主文档 §7 验收清单逐项可勾选
- [ ] 已补写 `collaboration/handoffs/phase7-mock-wework-testbed-dev-report.md`（产出物清单 + 自测结果）
