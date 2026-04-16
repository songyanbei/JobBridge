# Phase 4 开发 Checklist

> 基于：`collaboration/features/phase4-main.md`
> 配套实施文档：`collaboration/features/phase4-dev-implementation.md`
> 面向角色：后端开发
> 状态：`draft`
> 创建日期：2026-04-14

## A. 基线确认

- [ ] 已阅读 `collaboration/features/phase4-main.md`
- [ ] 已阅读 `collaboration/features/phase4-dev-implementation.md`
- [ ] 已确认本阶段只做 webhook / worker / message_router / 命令执行器，不做 admin API / 前端 / 外部定时任务
- [ ] 已确认 Phase 3 的 7 个 service 应直接调用，不在 message_router 中重写业务逻辑
- [ ] 已确认 webhook 绝不同步调 message_router，必须走 Redis 队列异步
- [ ] 已确认 Worker 是独立进程（`python -m app.services.worker`），不是后台线程
- [ ] 已确认出站回复必须经过 `permission_service` 过滤后再发送

## B. Webhook 端点（`api/webhook.py`）

涉及文件：

- `backend/app/api/webhook.py`（新建）
- `backend/app/main.py`（修改）

### B.1 路由与注册

- [ ] `webhook.py` 已创建
- [ ] `GET /webhook/wecom` 验证端点已实现
- [ ] `POST /webhook/wecom` 消息接收端点已实现
- [ ] 路由已注册到 `main.py`（`app.include_router(webhook_router)`）

### B.2 验签与解密

- [ ] 调用 `wecom/crypto.py` 的 `verify_signature()` 验签
- [ ] 验签失败返回 HTTP 403
- [ ] 调用 `wecom/crypto.py` 的 `decrypt_message()` 解密
- [ ] 解密失败返回 HTTP 200（避免企微重试），记录 error log
- [ ] 调用 `wecom/callback.py` 的 `parse_message()` 解析消息

### B.3 幂等检查

- [ ] Redis `SETNX msg:{MsgId} 1 EX 600` 幂等检查已实现
- [ ] MsgId 已存在时直接返回 200，不入队
- [ ] Redis 不可用时降级为 MySQL UNIQUE 约束兜底
- [ ] 幂等窗口 TTL 为 600 秒（10 分钟）

### B.4 用户级限流

- [ ] 限流检查在幂等检查之后、入队之前执行
- [ ] 调用 `check_rate_limit(external_userid, window, max_count)`
- [ ] 限流参数从 `system_config` 读取（`rate_limit.window_seconds`、`rate_limit.max_count`）
- [ ] 限流参数有缓存机制（避免每次请求查 DB）
- [ ] 被限流消息不写入 `wecom_inbound_event`
- [ ] 被限流时异步回复限流提示
- [ ] 被限流时返回 HTTP 200

### B.5 入站事件记录与入队

- [ ] 通过限流后写入 `wecom_inbound_event` 表，状态为 `received`
- [ ] 入队消息格式为 JSON，包含 `msg_id`、`from_userid`、`msg_type`、`content`、`media_id`、`create_time`、`inbound_event_id`
- [ ] 入队方式为 `RPUSH queue:incoming`
- [ ] 写入 inbound_event 失败不阻塞入队
- [ ] 入队失败不阻塞返回 200

### B.6 性能

- [ ] webhook 端点整体响应时间 < 100ms
- [ ] 不含任何 LLM 调用
- [ ] 不含任何复杂 DB 查询
- [ ] 不含任何同步业务处理

## C. Worker 进程（`services/worker.py`）

涉及文件：

- `backend/app/services/worker.py`（新建）

### C.1 进程基础

- [ ] `worker.py` 已创建
- [ ] 支持 `python -m app.services.worker` 启动
- [ ] 初始化时连接 DB 和 Redis
- [ ] 捕获 SIGTERM / SIGINT 信号
- [ ] 优雅退出：收到信号后完成当前消息处理再退出

### C.2 心跳

- [ ] 心跳线程已实现
- [ ] 每 60 秒写 Redis `worker:heartbeat:{pid}` TTL=120s
- [ ] 心跳线程为 daemon 线程
- [ ] Worker 退出时心跳线程一并退出

### C.3 启动自检

- [ ] 启动时查询 `wecom_inbound_event` 中 `status=processing` 的记录
- [ ] 将这些记录重新入队 `queue:incoming`
- [ ] 将这些记录状态重置为 `received`

### C.4 主循环

- [ ] `BLPOP queue:incoming` 阻塞消费，超时 5 秒
- [ ] 超时无消息时检查 `queue:send_retry`
- [ ] 消息反序列化为字典
- [ ] 每次消息处理使用独立 DB session（避免连接泄漏）

### C.5 消息处理流程

- [ ] 获取 userid 分布式锁（`session_lock:{userid}` TTL=30s）
- [ ] 锁获取失败时延迟重入队列
- [ ] 更新 `wecom_inbound_event` → `processing`，记录 `worker_started_at`
- [ ] 构造 `WeComMessage` 对象
- [ ] 调用 `message_router.process(msg)` 获取回复列表
- [ ] 依次调用 `WeComClient.send_text()` 发送回复
- [ ] 写入 `conversation_log`（入站 + 出站）
- [ ] `conversation_log` 必须包含 `wecom_msg_id`
- [ ] 更新 `wecom_inbound_event` → `done`，记录 `worker_finished_at`
- [ ] 释放 userid 分布式锁

### C.6 错误处理

- [ ] 处理异常时更新 `wecom_inbound_event.retry_count`
- [ ] `retry_count < 2` → 重入 `queue:incoming`
- [ ] `retry_count >= 2` → 入 `queue:dead_letter`
- [ ] 死信消息更新 `wecom_inbound_event` 状态为 `dead_letter`
- [ ] `error_message` 写入 `wecom_inbound_event`
- [ ] 死信消息尝试回复"系统繁忙，请稍后再试"
- [ ] Worker 进程不会因为单条消息异常而崩溃

### C.7 出站重试

- [ ] `queue:send_retry` 消费逻辑已实现
- [ ] 优先消费 `queue:incoming`，空闲时才处理 `queue:send_retry`
- [ ] 支持指数退避（60s / 120s / 300s）
- [ ] 重试 3 次仍失败 → 写 `audit_log`
- [ ] access_token 过期自动刷新重试
- [ ] API 限流入 `queue:send_retry`
- [ ] 用户不存在 → 标记 `inactive`，不重试

## D. 消息路由（`services/message_router.py`）

涉及文件：

- `backend/app/services/message_router.py`（新建）

### D.1 入口方法

- [ ] `message_router.py` 已创建
- [ ] `process(msg) -> list[ReplyMessage]` 入口方法已实现
- [ ] `ReplyMessage` 数据类已定义（userid, content, msg_type）
- [ ] message_router 不直接 import `wecom/client`（图片下载由 Worker 完成）

### D.2 用户识别与状态拦截

- [ ] 调用 `user_service.identify_or_register(msg.from_userid)`
- [ ] `status=blocked` 用户返回封禁提示
- [ ] `status=deleted` 用户返回删除状态提示
- [ ] 正常用户更新 `last_active_at`

### D.3 消息类型分流

- [ ] 文本消息 → `_handle_text()`
- [ ] 图片消息 → `_handle_image()`
- [ ] 语音消息 → 回复"暂不支持语音，请发送文字"
- [ ] 文件消息 → 回复"暂不支持文件，请直接用文字描述"
- [ ] event 类型 → 记录日志，返回空列表

### D.4 文本主链路

- [ ] 首次欢迎判定（`user_info.should_welcome`）优先于意图分类
- [ ] 调用 `intent_service.classify_intent(text, role, history, current_criteria)` 统一识别（内含命令→show_more→LLM三级优先）
- [ ] `intent=="command"` → 从 `structured_data["command"]` 取归并 key → `_handle_command()`
- [ ] `upload_job` / `upload_resume` → 调用 `upload_service`
- [ ] `search_job` / `search_worker` → criteria 更新 + 检索 + 权限过滤 + 格式化
- [ ] `upload_and_search` → 先上传后检索
- [ ] `follow_up` → patch merge + 重新检索
- [ ] `show_more` → `search_service.show_more(session, user_ctx, db)`
- [ ] `chitchat` → 返回引导语
- [ ] 未知意图 → 返回兜底提示

### D.5 图片消息处理

- [ ] 图片下载由 Worker 层完成（`WeComClient.download_media()` + storage 存储），message_router 不直接调用 wecom/client
- [ ] message_router 中 `_handle_image()` 通过 `msg.image_url` 获取 Worker 已保存的图片 URL
- [ ] 上传流程中 → 关联到当前岗位/简历
- [ ] 非上传流程 → 回复"图片已收到，作为附件留存"
- [ ] 一期不做 OCR

### D.6 回复格式

- [ ] 工人视角检索结果格式符合方案设计 §10.5
- [ ] 厂家/中介视角检索结果格式符合方案设计 §10.5
- [ ] 所有出站文本经过 `permission_service.filter_fields()` 过滤
- [ ] 工人侧不展示电话和详细地址
- [ ] 每批展示 3 条，底部附引导语

## E. 命令执行器

### E.1 命令路由

- [ ] 命令路由表已定义（10 条命令，含 `/人工客服`）
- [ ] 同义词映射已覆盖（方案设计 §17.2.2 定义的同义词）
- [ ] 未知命令返回兜底提示

### E.2 各命令实现

- [ ] `/帮助` 返回帮助文案
- [ ] `/重新找` 清空 session 中 criteria/snapshot/shown_items
- [ ] `/重新找` 无活跃 session 时回复"当前没有可清空的搜索条件"
- [ ] `/找岗位` 检查角色为中介
- [ ] `/找岗位` 非中介回复"只有中介账号可以切换双向模式"
- [ ] `/找工人` 同上
- [ ] `/续期` 支持无参数（默认 15 天）和带参数（`/续期 15`、`/续期 30`）
- [ ] `/续期` 多个岗位时返回列表
- [ ] `/续期` 无岗位时回复异常文案
- [ ] `/下架` 设置 `delist_reason=manual_delist`
- [ ] `/下架` 无在线岗位时回复异常文案
- [ ] `/招满了` 设置 `delist_reason=filled`
- [ ] `/招满了` 无在线岗位时回复异常文案
- [ ] `/删除我的信息` 检查角色为工人
- [ ] `/删除我的信息` 执行软删除简历+日志+session
- [ ] `/删除我的信息` 更新 user.status=deleted
- [ ] `/人工客服` 返回人工客服联系方式引导文案
- [ ] `/人工客服` 无角色和前置条件限制
- [ ] `/我的状态` 返回账号和提交状态摘要

## F. 部署配置

- [ ] `docker-compose.yml` 已增加 worker 服务
- [ ] `docker-compose.prod.yml` 已增加 worker 服务
- [ ] Worker 服务 `depends_on` 包含 mysql 和 redis
- [ ] Worker 服务使用 `restart: unless-stopped`
- [ ] Worker 服务 command 为 `python -m app.services.worker`
- [ ] 如有新增 `system_config` key，已同步更新 `seed.sql`

## G. 对话日志

- [ ] 入站消息写入 `conversation_log`（direction="in"）
- [ ] 出站回复写入 `conversation_log`（direction="out"）
- [ ] **仅入站消息**写入 `wecom_msg_id`（UNIQUE 约束）
- [ ] 出站回复 `wecom_msg_id` 为 NULL
- [ ] `criteria_snapshot` 记录 prompt 版本
- [ ] 多条出站回复每条单独记录

## H. 回复文案常量

- [ ] `BLOCKED_REPLY` 已定义
- [ ] `DELETED_REPLY` 已定义
- [ ] `VOICE_NOT_SUPPORTED` 已定义
- [ ] `FILE_NOT_SUPPORTED` 已定义
- [ ] `RATE_LIMITED_REPLY` 已定义
- [ ] `SYSTEM_BUSY_REPLY` 已定义
- [ ] `HUMAN_AGENT_REPLY` 已定义
- [ ] `FALLBACK_REPLY` 已定义
- [ ] 帮助文案已定义
- [ ] 欢迎语已定义（工人/厂家/中介三种）

## I. 自动化测试

- [ ] webhook 验签成功/失败单测
- [ ] webhook 幂等检查单测
- [ ] webhook 限流检查单测
- [ ] Worker 消息消费单测（mock message_router）
- [ ] Worker 错误重试单测
- [ ] Worker 死信处理单测
- [ ] message_router 消息类型分流单测
- [ ] message_router 意图分发单测
- [ ] 命令路由单测（10 条命令，含 `/人工客服`）
- [ ] 出站失败补偿单测
- [ ] E2E：新工人首次消息 → 注册 → 欢迎语
- [ ] E2E：工人求职 → 推荐 Top 3
- [ ] E2E：厂家发布岗位 → 入库成功
- [ ] E2E：中介切换方向

## J. 收尾确认

- [ ] 无同步阻塞式 webhook 实现残留
- [ ] 无 message_router 直接发送消息的代码
- [ ] 无硬编码限流参数
- [ ] 无 DB session 泄漏风险
- [ ] Worker 进程可独立启停
- [ ] Worker 进程优雅退出正常
- [ ] 队列状态可通过 Redis CLI 观测
- [ ] inbound_event 状态流转可通过 MySQL 查询观测
