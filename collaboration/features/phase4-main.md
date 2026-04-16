# Feature: Phase 4 企微消息主链路

> 状态：`draft`
> 创建日期：2026-04-14
> 对应实施阶段：Phase 4
> 关联实施文档：`docs/implementation-plan.md` §4.5
> 关联方案设计章节：§2.3、§8.2、§10.1、§10.4、§10.5、§11、§12、§17.2
> 关联架构章节：`docs/architecture.md` §三、§五
> 配套文档：
> - 开发实施文档：`collaboration/features/phase4-dev-implementation.md`
> - 开发 Checklist：`collaboration/features/phase4-dev-checklist.md`
> - 测试实施文档：`collaboration/features/phase4-test-implementation.md`
> - 测试 Checklist：`collaboration/features/phase4-test-checklist.md`

## 1. 阶段目标

Phase 4 的目标，是把系统最核心的用户入口打通，形成"企微收消息 → 异步处理 → 回复用户"的完整闭环。

本阶段完成后，项目至少应具备以下能力：

- 企微 webhook 可稳定接收消息并在 100ms 内返回 200
- 限流、幂等、入站事件表三层防线均已就位
- 独立 Worker 进程能从 Redis 队列消费消息并完成完整业务处理
- message_router 能正确分流文本（意图分发）、图片（留存）、语音/文件（拒绝）
- 工人、厂家、中介三类用户通过企微发消息可走完典型业务流程
- 全部一期命令（`/帮助`、`/重新找`、`/找岗位`、`/找工人`、`/续期`、`/下架`、`/招满了`、`/删除我的信息`、`/人工客服`、`/我的状态`，共 10 条）可在消息链路中正确执行
- 对话日志写入完整，含 `wecom_msg_id`
- 出站失败有重试和补偿机制
- Worker 自写心跳，可观测数据（队列长度、死信数）可通过 Redis CLI 查询
- docker-compose 中 Worker 以独立服务配置

## 2. 当前代码现状

当前仓库内与 Phase 4 直接相关的基础已经具备：

- `backend/app/wecom/crypto.py`：验签、加解密已完成（Phase 2 交付）
- `backend/app/wecom/callback.py`：`WeComMessage` 数据类、XML 解析已完成（Phase 2 交付）
- `backend/app/wecom/client.py`：`WeComClient` 含 token 管理、`send_text()`、`download_media()` 已完成（Phase 2 交付）
- `backend/app/services/`：7 个业务 service 已完成（Phase 3 交付）
  - `user_service.py`：用户识别、注册、封禁/删除拦截
  - `intent_service.py`：意图分类、结构化抽取、追问
  - `conversation_service.py`：会话状态、criteria merge、快照、show_more
  - `audit_service.py`：敏感词检测、审核队列判定
  - `search_service.py`：硬过滤、候选集、rerank、权限过滤
  - `upload_service.py`：岗位/简历入库编排
  - `permission_service.py`：字段级权限过滤
- `backend/app/core/redis_client.py`：session、分布式锁、限流、幂等、队列基础能力已完成
- `backend/app/llm/prompts.py`：业务版 prompt 已定稿（Phase 3 交付）
- `backend/app/config.py`：含企微配置项（corp_id / agent_id / secret / token / aes_key）
- `docs/message-contract.md`：企微消息基础契约已锁定

当前缺失的部分：

- `backend/app/api/webhook.py`：webhook 端点不存在
- `backend/app/services/worker.py`：Worker 进程不存在
- `backend/app/services/message_router.py`：消息路由不存在
- `docker-compose.yml` / `docker-compose.prod.yml` 中没有 worker 服务
- `backend/app/api/` 目录下尚无路由注册到 `main.py`
- 没有命令路由和执行模块（`/续期`、`/下架`、`/招满了` 等命令的消息链路接入）
- 没有出站失败补偿队列和重试逻辑
- 没有 Worker 心跳和告警逻辑

## 3. 本阶段范围

### 3.1 本阶段必须完成

#### 模块 A：`api/webhook.py` — Webhook 端点

- 实现 `POST /webhook/wecom` 端点
- 实现 `GET /webhook/wecom` 用于企微回调 URL 验证（返回 echostr）
- 验签（调用 `wecom/crypto.py` 的 `verify_signature()`）
- 解密（调用 `wecom/crypto.py` 的 `decrypt_message()`）
- 解析消息（调用 `wecom/callback.py` 的 `parse_message()`）
- MsgId 幂等检查（Redis `SETNX msg:{MsgId} 1 EX 600`）
- 用户级限流（`check_rate_limit(userid, window=10, max_count=5)`）
- 被限流消息不写入 `wecom_inbound_event`，异步回复限流提示
- 写入 `wecom_inbound_event` 表，状态为 `received`
- 消息入队（`RPUSH queue:incoming`）
- 立即返回 HTTP 200 "success"（目标 < 100ms）
- webhook 端点注册到 `main.py` 的 FastAPI app

处理顺序（严格）：

```
验签 → 解密 → 解析 → 幂等检查 → 限流检查 → 写入 inbound_event → 入队 → 返回 200
```

#### 模块 B：`services/worker.py` — 异步 Worker 进程

- 作为独立进程运行（`python -m app.services.worker`）
- `BLPOP queue:incoming` 阻塞消费
- 消费到消息后：
  1. 将 `wecom_inbound_event` 状态更新为 `processing`，记录 `worker_started_at`
  2. 获取 userid 分布式锁（`SETNX session_lock:{userid}` TTL=30s），保证同一用户消息串行处理
  3. 调用 `message_router.process(msg)`
  4. 回复发送（调用 `WeComClient.send_text()`）
  5. 写入 `conversation_log`（含 `wecom_msg_id`）
  6. 更新 `wecom_inbound_event` 状态为 `done`，记录 `worker_finished_at`
  7. 释放 userid 分布式锁
- 错误处理：
  - 处理异常时最多重试 2 次（消息重入 `queue:incoming`）
  - 重试计数通过 `wecom_inbound_event.retry_count` 跟踪
  - 2 次仍失败 → 入死信队列 `queue:dead_letter`，更新状态为 `dead_letter`
  - 死信消息给用户回复"系统繁忙，请稍后再试"
- 心跳上报：每 60 秒写 Redis `worker:heartbeat:{pid}` TTL=120 秒
- 启动时自检：检查 `wecom_inbound_event` 表中 `status=processing` 的消息（Worker crash 后恢复）
- 优雅退出：捕获 SIGTERM/SIGINT，完成当前消息处理后退出

#### 模块 C：`services/message_router.py` — 消息路由

- 入口方法：`process(msg: WeComMessage) -> list[ReplyMessage]`
- 处理流程：
  1. 用户识别：调用 `user_service.identify_or_register()`
  2. 封禁/删除拦截：根据返回状态短路
  3. 消息类型分流：
     - **文本消息** → 进入文本主链路
     - **图片消息** → 调用 `storage` 下载留存，回复确认
     - **语音消息** → 回复"暂不支持语音，请发送文字"
     - **文件消息** → 回复"暂不支持文件，请直接用文字描述"
     - **event 类型** → 记录日志，不做业务处理
  4. 文本主链路：
     a. 显式命令优先检测（Phase 3 `intent_service` 已有此逻辑）
     b. 命令分流：`/帮助`、`/重新找`、`/找岗位`、`/找工人`、`/续期`、`/下架`、`/招满了`、`/删除我的信息`、`/人工客服`、`/我的状态`
     c. 非命令文本 → 调用 `intent_service.classify_intent()` 获取意图
     d. 按意图分发：
        - `upload_job` / `upload_resume` → `upload_service`
        - `search_job` / `search_worker` → `search_service`
        - `upload_and_search` → 先 `upload_service` 再 `search_service`
        - `follow_up` → `conversation_service` patch merge + `search_service`
        - `show_more` → `search_service.show_more()`
        - `chitchat` → 返回引导语
  5. 更新 `last_active_at`

#### 模块 D：命令执行器

基于 Phase 3 已有的 service 基座，接入完整命令路由：

| 命令 | 可用角色 | 处理逻辑 |
|---|---|---|
| `/帮助` | 全部 | 返回帮助文案 |
| `/重新找` | 全部 | 清空 session 中 `search_criteria`、`candidate_snapshot`、`shown_items` |
| `/找岗位` | 中介 | 设置会话方向为 `search_job` |
| `/找工人` | 中介 | 设置会话方向为 `search_worker` |
| `/续期` | 厂家/中介 | 默认续期最近一个未过期岗位；支持 `/续期 15` `/续期 30`；多个岗位时列表让用户选 |
| `/下架` | 厂家/中介 | 下架岗位（`delist_reason=manual_delist`） |
| `/招满了` | 厂家/中介 | 标记招满（`delist_reason=filled`） |
| `/删除我的信息` | 工人 | 软删除简历+对话日志+会话状态，`user.status` 标记 `deleted` |
| `/人工客服` | 全部 | 返回人工客服联系方式，引导转人工 |
| `/我的状态` | 全部 | 返回账号状态和最近提交状态 |

#### 模块 E：对话日志与出站补偿

- `conversation_log` 写入：
  - 入站消息和出站回复均须记录
  - **仅入站消息**写入 `wecom_msg_id`（因为 `wecom_msg_id` 字段有 UNIQUE 约束，见 `models.py` line 244；出站回复不写此字段，设为 NULL）
  - `criteria_snapshot` 记录当前 prompt 版本
- 出站失败补偿：
  - 网络超时 / 5xx → 立即重试 1 次，仍失败写 `queue:send_retry`
  - access_token 过期 → 自动刷新重试
  - API 限流 → 写 `queue:send_retry`，指数退避（60s / 120s / 300s）
  - 用户不存在 / 已退出 → 不重试，标记 `inactive`
  - 重试 3 次仍失败 → 入 `audit_log`

#### 模块 F：部署配置

- `docker-compose.yml`（开发环境）增加 worker 服务配置
- `docker-compose.prod.yml` 增加 worker 服务：

```yaml
worker:
  build: ./backend
  command: python -m app.services.worker
  depends_on:
    - mysql
    - redis
  env_file: .env
  restart: unless-stopped
```

- `main.py` 注册 webhook 路由

#### 模块 G：可观测性基础

Phase 4 只做 Worker 自身产出的可观测数据，**不做外部定时巡检和群告警推送**（留给 Phase 7）：

- **Worker 自写心跳**：每 60 秒写 Redis `worker:heartbeat:{pid}` TTL=120s
- **可观测数据暴露**：队列长度（`LLEN queue:incoming`）、死信数量（`LLEN queue:dead_letter`）可通过 Redis CLI 查询
- **错误日志记录**：所有处理异常、死信、出站失败均写入 `wecom_inbound_event.error_message` 和应用日志

以下能力留给 Phase 7：
- 外部定时任务巡检（每 3 分钟检查 heartbeat、每分钟检查队列积压）
- 告警写入 `audit_log` 并推送企微群

### 3.2 本阶段明确不做

- 不实现 admin API（Phase 5 范围）
- 不实现前端页面或前后端联调（Phase 6 范围）
- 不实现外部定时任务（TTL 清理、日报、心跳巡检、队列积压告警推送等留给 Phase 7）
- 不引入向量检索、RAG、知识库
- 不支持语音识别、文件解析、图片 OCR
- 不实现 headcount 自动递减
- 不实现自动封禁规则
- 不把 webhook 改成同步直接调 message_router
- 不以内嵌线程替代独立 Worker 进程
- 不支持一期未定义的媒体类型
- 不跳过限流检查直接入队
- 不跳过幂等检查
- 不把失败消息直接吞掉不记录

特别说明：

- Phase 4 只做 Worker 自写心跳和 `queue:send_retry` 消费（Worker 内置能力），不做外部定时巡检（Phase 7 范围）
- 如 Phase 4 过程中发现 `upload_service` 缺少 `attach_image()` 方法，允许在 `upload_service.py` 中新增该方法

## 4. 真值来源与实现基线

出现冲突时，按以下优先级执行：

1. `docs/implementation-plan.md` §4.5
2. `方案设计_v0.1.md` §12（企微接入）、§10.1（意图分流）、§11（多轮对话）、§17.2（命令表）
3. `docs/architecture.md` §三、§五
4. `docs/message-contract.md`
5. `collaboration/features/phase3-main.md`（Phase 3 handoff）
6. 本文档

本阶段额外锁定以下实现约束：

- webhook 端点绝对不能同步调用 `message_router`，必须走 Redis 队列异步
- Worker 必须作为独立进程部署，不能是后台线程或 APScheduler 任务
- 同一 userid 的消息处理必须串行（Redis 分布式锁保证）
- 被限流消息不写入 `wecom_inbound_event`（不消耗存储）
- 限流参数（窗口和最大次数）可通过 `system_config` 表配置
- 出站回复必须经过 `permission_service` 过滤后再发送，不能直接透传 LLM 输出
- `conversation_log` 必须包含 `wecom_msg_id` 字段

### 4.1 Phase 4 依赖的 `system_config` key

| key | 用途 | 默认值来源 |
|---|---|---|
| `rate_limit.window_seconds` | 限流窗口时长（秒） | `backend/sql/seed.sql`，默认 10 |
| `rate_limit.max_count` | 限流窗口内最大消息数 | `backend/sql/seed.sql`，默认 5 |
| `match.top_n` | 首轮推荐条数（复用 Phase 3） | `backend/sql/seed.sql` |

如需新增 key，必须同步更新 `backend/sql/seed.sql`。

## 5. 详细需求说明

### 5.1 `api/webhook.py`

涉及文件：

- `backend/app/api/webhook.py`（新建）
- `backend/app/main.py`（修改，注册路由）

要求：

- GET `/webhook/wecom`：接受企微回调 URL 验证请求，验签后返回 `echostr`
- POST `/webhook/wecom`：接受企微回调消息推送
- 验签失败返回 HTTP 403
- 解密失败返回 HTTP 200（避免企微重试），记录错误日志
- 幂等检查：Redis `SETNX msg:{MsgId} 1 EX 600`
  - MsgId 已存在 → 直接返回 200，不入队
  - Redis 不可用 → 降级为 MySQL `wecom_inbound_event` UNIQUE 约束
- 限流检查：`check_rate_limit(external_userid, window, max_count)`
  - 超限 → 不入队，不写 `wecom_inbound_event`，异步回复限流提示，返回 200
  - 限流参数从 `system_config` 读取
- 写入 `wecom_inbound_event` 表（status=received）
- 入队：`RPUSH queue:incoming` （消息序列化为 JSON）
- 返回 HTTP 200 `"success"`

性能要求：

- webhook 端点从收到请求到返回 200 的时间 < 100ms
- 不能在 webhook 中做任何 LLM 调用、数据库复杂查询或同步业务处理

### 5.2 `services/worker.py`

涉及文件：

- `backend/app/services/worker.py`（新建）

要求：

- 入口为 `if __name__ == "__main__"` 或 `main()` 函数，支持 `python -m app.services.worker` 启动
- 初始化时：
  - 连接 DB 和 Redis
  - 启动心跳线程（每 60 秒写 `worker:heartbeat:{pid}` TTL=120s）
  - 执行启动自检（检查 `wecom_inbound_event` 中 `status=processing` 的消息并重新入队）
- 主循环：
  - `BLPOP queue:incoming`（阻塞超时建议 5 秒，超时后继续循环以检查退出信号）
  - 反序列化消息
  - 获取 userid 分布式锁（`session_lock:{userid}`，TTL=30s）
  - 更新 `wecom_inbound_event` → `processing`
  - 调用 `message_router.process(msg)`
  - 处理回复列表（可能多条）：依次调用 `WeComClient.send_text()`
  - 写入 `conversation_log`
  - 更新 `wecom_inbound_event` → `done`
  - 释放锁
- 错误处理：
  - 捕获异常 → `wecom_inbound_event.retry_count += 1`
  - `retry_count < 2` → 重入 `queue:incoming`
  - `retry_count >= 2` → 入 `queue:dead_letter`，状态更新为 `dead_letter`
  - 记录 `error_message` 到 `wecom_inbound_event`
  - 死信消息异步回复"系统繁忙，请稍后再试"
- 出站失败补偿：
  - Worker 同时消费 `queue:send_retry`（低优先级，`queue:incoming` 为空时才处理）
  - 每条 retry 消息含退避时间，未到退避时间的消息重入队列
- 优雅退出：
  - 捕获 SIGTERM / SIGINT
  - 设置退出标志
  - 当前消息处理完毕后退出循环
  - 心跳线程一并退出

### 5.3 `services/message_router.py`

涉及文件：

- `backend/app/services/message_router.py`（新建）

要求：

- 公共入口：`process(msg: WeComMessage) -> list[ReplyMessage]`
- `ReplyMessage` 数据类：至少包含 `userid`、`content`、`msg_type`（text / image / link）
- 处理链路（顺序执行）：
  1. `user_service.identify_or_register(msg.from_userid)` → 获取用户信息
  2. 判断用户状态：
     - `status=blocked` → 返回封禁提示
     - `status=deleted` → 返回删除状态提示
  3. 更新 `last_active_at`
  4. 按 `msg.msg_type` 分流：
     - `text` → `_handle_text(msg, user)`
     - `image` → `_handle_image(msg, user)`
     - `voice` → 返回不支持提示
     - `file` → 返回不支持提示
     - `event` → 记录日志，返回空
  5. 返回回复列表

文本处理链路 `_handle_text(msg, user)`：

1. 显式命令检测（前缀匹配 + 同义词映射，Phase 3 `intent_service` 已实现）
2. 如果是命令 → `_handle_command(cmd, msg, user)`
3. 如果首次交互 → 返回欢迎语（`user_service` 判断）
4. 非命令文本 → 调用 `intent_service.classify(msg.content, user, session)`
5. 按意图分发到具体处理函数

各意图处理函数的编排：

| 意图 | 处理函数 | 编排逻辑 |
|---|---|---|
| `upload_job` | `_handle_upload()` | 调用 `upload_service.process_upload()` → 回复入库结果或追问 |
| `upload_resume` | `_handle_upload()` | 同上 |
| `search_job` | `_handle_search()` | 调用 `conversation_service` 保存 criteria → `search_service.search()` → `permission_service.filter()` → 格式化回复 |
| `search_worker` | `_handle_search()` | 同上 |
| `upload_and_search` | `_handle_upload_and_search()` | 先上传后检索 |
| `follow_up` | `_handle_follow_up()` | `conversation_service.merge_criteria_patch()` → `search_service.search_jobs()` / `search_workers()` |
| `show_more` | `_handle_show_more()` | `search_service.show_more()` |
| `chitchat` | `_handle_chitchat()` | 返回引导语 |

### 5.4 命令执行器

涉及文件：

- `backend/app/services/message_router.py` 中的 `_handle_command()` 方法
- 或独立为 `backend/app/services/command_service.py`（开发可自行决定）

命令路由表（一期完整命令集）：

| 命令 | 同义词 | 可用角色 | 前置条件 | 正常回复 | 异常回复 | 副作用 |
|---|---|---|---|---|---|---|
| `/帮助` | 帮助、怎么用、指令 | 全部 | 无 | 帮助文案 | n/a | 无 |
| `/重新找` | 重来、重新搜、清空条件 | 全部 | 有活跃 session | 清空确认 | "当前没有可清空的搜索条件" | 清空 criteria/snapshot/shown_items |
| `/找岗位` | 帮我找工作、切到找岗位 | 中介 | user.role=broker | 切换确认 | "只有中介账号可以切换双向模式" | session.broker_direction=search_job |
| `/找工人` | 帮我招人、切到找工人 | 中介 | user.role=broker | 切换确认 | "只有中介账号可以切换双向模式" | session.broker_direction=search_worker |
| `/续期` | 延期、续15天、续30天 | 厂家/中介 | 用户名下存在未删除岗位 | "已为您将岗位【xxx】续期 N 天" | "未找到可续期的岗位" | 更新 job.expires_at |
| `/下架` | 岗位下架、先不招了、暂停招聘 | 厂家/中介 | 用户名下存在在线岗位 | "已将岗位【xxx】下架" | "未找到可下架的岗位" | job.delist_reason=manual_delist |
| `/招满了` | 招满了、人招够了、满员了 | 厂家/中介 | 用户名下存在在线岗位 | "已标记岗位【xxx】为招满状态" | "未找到可操作的岗位" | job.delist_reason=filled |
| `/删除我的信息` | 删除信息、清空我的资料、注销 | 工人 | 用户存在 | "已收到您的删除请求" | "未找到可删除的数据" | 软删除简历+日志+session，status=deleted |
| `/人工客服` | 客服、转人工、联系人工 | 全部 | 无 | "已为您转人工客服，请稍候；也可直接联系 xxx。" | n/a | 无 |
| `/我的状态` | 我的账号状态、我被封了吗 | 全部 | 用户存在 | 状态摘要 | "未找到您的账号记录" | 无 |

续期命令细则：

- `/续期` 无参数 → 默认对最近一个未过期岗位续期 15 天
- 用户名下有多个未过期岗位 → 返回编号列表请用户确认
- `/续期 15` 或 `/续期 30` → 明确续期天数
- 续期不能超过 TTL 上限（`ttl.job.days` * 2）

### 5.5 对话日志写入规范

- 每次消息处理必须写入至少两条 `conversation_log`：入站消息 + 出站回复
- **`wecom_msg_id` 仅入站消息写入**（`models.py` 中该字段有 `unique=True` 约束，出站回复如果也写相同值会撞库）
- 出站回复的 `wecom_msg_id` 字段留 NULL
- `criteria_snapshot` 记录当前的 search_criteria 和 prompt 版本
- 如果出站有多条回复（如追问+引导），每条单独记录

### 5.6 图片消息处理

- 收到图片消息时：
  1. **图片下载由 Worker 层完成**（Worker 调用 `WeComClient.download_media(media_id)` 并存入 `storage`），message_router 不直接依赖 `wecom/client`
  2. Worker 将已保存的图片 URL 附加到消息对象后再传入 `message_router.process()`
  3. message_router 中的 `_handle_image()` 只负责业务逻辑：
     - 如果用户当前在上传流程中 → 关联到当前岗位/简历的 `images` 字段（通过 `upload_service` 已有接口，如需新增 `attach_image()` 方法则在 Phase 4 补充）
     - 如果不在上传流程 → 回复"图片已收到。一期仅支持文字描述发布信息，图片作为附件留存"
  4. 一期不做 OCR，图片不参与结构化抽取

## 6. 接口契约

### 6.1 新增 API 端点

```
GET /webhook/wecom?msg_signature=xxx&timestamp=xxx&nonce=xxx&echostr=xxx
→ 200: echostr 明文

POST /webhook/wecom?msg_signature=xxx&timestamp=xxx&nonce=xxx
Body: XML 加密消息体
→ 200: "success"
```

### 6.2 内部数据流契约

入队消息格式（JSON 序列化后 RPUSH 到 `queue:incoming`）：

```json
{
  "msg_id": "企微 MsgId",
  "from_userid": "external_userid",
  "msg_type": "text",
  "content": "消息内容",
  "media_id": "图片 media_id（仅图片消息）",
  "create_time": 1712000000,
  "inbound_event_id": "wecom_inbound_event 表主键"
}
```

回复消息格式（`ReplyMessage` 数据类）：

```python
@dataclass
class ReplyMessage:
    userid: str          # 接收者 external_userid
    content: str         # 回复文本
    msg_type: str = "text"  # 一期固定 text
```

### 6.3 wecom_inbound_event 状态流转

```
received → processing → done
                     ↘ failed → (retry) → processing
                                        ↘ dead_letter
```

## 7. 验收标准

- [ ] webhook 可稳定接收企微消息并在 100ms 内返回 200
- [ ] 限流生效：同一用户 10 秒内超过 5 条消息被限流
- [ ] 幂等生效：重复 MsgId 不会重复处理
- [ ] `wecom_inbound_event` 状态流转正确（received → processing → done/failed/dead_letter）
- [ ] Worker 以独立进程运行（`python -m app.services.worker`）
- [ ] Worker 启动自检恢复 `processing` 状态消息
- [ ] Worker 心跳每 60 秒上报
- [ ] 同一 userid 消息串行处理（分布式锁生效）
- [ ] 新工人首次消息 → 自动注册 → 欢迎语
- [ ] 工人求职 → 推荐 Top 3（格式符合 §10.5）
- [ ] 厂家发布岗位 → 入库成功 → 回复确认
- [ ] 中介切换 `/找岗位` `/找工人` 正确
- [ ] 10 条命令全部可正确执行
- [ ] 语音/文件消息返回不支持提示
- [ ] 图片消息留存成功
- [ ] 对话日志写入完整，入站消息含 `wecom_msg_id`，出站回复 `wecom_msg_id` 为 NULL
- [ ] 出站失败有重试和补偿
- [ ] 死信、重试、日志链路可观测
- [ ] docker-compose 中 worker 服务可正常启停
- [ ] 无同步阻塞式 webhook 实现残留

## 8. 进入条件

Phase 4 开始开发前必须确认以下前置条件：

| 条件 | 状态 | 说明 | 如未满足的应对 |
|---|---|---|---|
| **企微出站 API 权限已确认** | **待确认** | 方案设计 §12.2 明确"方案 A 是唯一能支撑完整异步业务的路径，必须在开发前确认"（§16.1 #11）。`WeComClient.send_text()` 依赖企微已认证 + 开通"客户联系"权限 | 如无法确认，Phase 4 代码按方案 A 实现，但 E2E 验收降级为 Mock 出站；企微真实联调推迟到权限就绪后 |
| Phase 3 service 层交付 | 待确认 | 7 个 service 已通过 Phase 3 验收 | Phase 4 无法开始 |
| 企微回调公网地址 | 待确认 | 回调 URL 需公网可达 | 开发阶段可用 Mock server 或内网穿透，但联调验收必须使用真实回调 |

## 9. 风险与备注

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| 企微认证级别未确认 | 出站方案可能受限，方案 C（被动回复）与异步模型冲突 | 代码按方案 A 实现，降级路径见 §12.2；如最终只能用方案 C，需回溯修改 webhook 为同步返回 |
| 企微回调公网地址未就绪 | 无法真实联调 | Mock server 模拟企微回调，或使用内网穿透工具 |
| LLM 响应慢导致 Worker 积压 | 队列增长、用户等待 | Worker 心跳 + 可观测数据已在本阶段实现；后续可 `docker compose up --scale worker=N` |
| Redis 宕机 | 队列丢失 | 有 `wecom_inbound_event` 表兜底恢复；幂等有三层防御 |

## 10. 文件变更清单

| 操作 | 文件 | 说明 |
|---|---|---|
| 新建 | `backend/app/api/webhook.py` | Webhook 端点 |
| 新建 | `backend/app/services/worker.py` | 异步 Worker 进程 |
| 新建 | `backend/app/services/message_router.py` | 消息路由编排 |
| 新建或修改 | `backend/app/services/command_service.py` | 命令执行器（可合并到 message_router） |
| 修改 | `backend/app/main.py` | 注册 webhook 路由 |
| 修改 | `docker-compose.yml` | 增加 worker 服务 |
| 修改 | `docker-compose.prod.yml` | 增加 worker 服务 |
| 可能修改 | `backend/app/core/redis_client.py` | 补充出站重试队列相关方法 |
| 可能修改 | `backend/app/schemas/` | 补充 ReplyMessage 等数据类 |
| 可能修改 | `backend/sql/seed.sql` | 补充限流相关 system_config key |
