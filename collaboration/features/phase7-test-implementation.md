# Phase 7 测试实施文档

> 基于：`collaboration/features/phase7-main.md`
> 面向角色：测试 + 运维协同
> 状态：`draft`
> 创建日期：2026-04-18
> 最近修订：2026-04-18（按 codex 评审反馈修订：scheduler 同进程、audit_log 走 loguru、User.extra.deleted_at、nginx SPA/API 分流、queue:group_send_retry、P95 口径、E11 降级、config key 命名）

## 1. 测试目标

Phase 7 测试的核心职责是把 Phase 1~6 的代码当作"准上线系统"整体检验，重点回答以下问题：

- 定时任务是否**在 app 进程内**稳定运行、幂等、可重入、可观测、横向扩容下不重复触发
- TTL 清理 / 7 天硬删是否按口径执行且不误伤未过期数据
- `/删除我的信息` 是否在 `User.extra['deleted_at']` 正确写入后于 7 天完成硬删闭环
- Worker 心跳 / 队列积压 / 死信 / 出站补偿告警是否准时触发、通过 loguru 结构化留痕、10 分钟内去重
- 每日日报是否按时推送运营群，失败是否进入 `queue:group_send_retry`（不污染 `queue:send_retry`）
- Docker **5 容器**编排（nginx + app + worker + mysql + redis）是否一键可拉起
- nginx `/admin/*` 对 axios JSON 与浏览器 HTML 请求分流是否正确
- 备份与恢复演练是否真实可用（不仅仅"有脚本"）
- E2E 完整链路（企微入站 → 推荐回复 → 后台审核 → 前端可视 → 事件回传 → 日报统计）是否稳定
- §17.1.4 的七项 MVP 指标是否在 §17.1.1 要求的 **至少 7 天试运营**后全部达标
- E11（小程序点击回传）按 §17.1.3 降级规则执行
- §14.5.4 上线前 Checklist、§17.3.3 5 项必关闭项是否全部完成

如任一项失败，Phase 7 视为未通过。

## 2. 当前现状与测试策略

Phase 7 的风险集中在：

1. **运行时风险**：定时任务、告警去重、Worker 重启、日报推送能否在 7×24 小时下稳定；横向扩容下的分布式锁有效性
2. **数据风险**：TTL 硬删误伤、用户被遗忘权未闭环、备份无法恢复、`User.extra['deleted_at']` 写入时机
3. **上线风险**：生产 `CORS_ORIGINS` / 默认密码 / 企微回调 / HTTPS / nginx `/admin/*` 分流错误导致首日事故

策略：

- 自动化优先覆盖定时任务、告警去重、nginx 分流：模拟时钟、构造边界条件、注入异常、Accept/method 矩阵
- 手工 / 脚本覆盖 Docker 编排、备份恢复、告警推送链路
- E2E 以"真实企微账号 + 真实后台操作"为主，Mock 降级仅允许因外部依赖未就绪
- 指标采集以**连续 7 天试运营**数据为准（§17.1.1）
- 发现问题回归到 Phase 1~6 对应主文档或 handoffs，不直接在 Phase 7 解决跨阶段缺陷

## 3. 测试范围

### 3.1 必测范围

1. APScheduler 同进程调度
2. 横向扩容分布式锁
3. TTL 清理 + 7 天硬删
4. `/删除我的信息` 闭环（`User.extra['deleted_at']` + ttl_cleanup 联动）
5. Worker 心跳 / 队列积压 / 死信巡检
6. 出站补偿巡检 + `queue:group_send_retry` 消费
7. 每日日报生成 + 企微群推送 + 昨日对比 + 失败补偿
8. LLM 调用日志
9. Docker **5 容器**编排 + `/health` + `/nginx-health`
10. nginx `/admin/*` 对 axios 与浏览器分流
11. `CORS_ORIGINS` 生产基线校验
12. 备份脚本 + 恢复演练
13. E2E E1~E13（E11 按降级规则）
14. §17.1.4 七项 MVP 指标（P95 口径按 dev-implementation §5.14）
15. §14.5.4 上线前 Checklist
16. §17.3.3 外部依赖必关闭项
17. Phase 4 / 5 / 6 关键路径回归

### 3.2 不测范围

- 不重复测 Phase 1~6 已验收的内部实现
- 不测二期能力（RBAC、RAG、Prometheus、K8s、headcount 自减、告警规则配置等）
- 不测 Phase 5 报表内部 SQL 细节
- 不做性能压测极限
- 不测 Phase 4 Worker 内部路由分支

## 4. 测试环境要求

### 4.1 环境矩阵

| 环境 | 用途 | 数据 |
|---|---|---|
| 开发 | 自测 / Mock 联调 | 开发 seed 假数据 |
| 预发 | 恢复演练 + 7 天试运营数据主战场 | 从生产备份导入 + 实时试运营 |
| 生产 | 上线前最后一轮验证 + 首次备份演练 | 正式种子数据 |

### 4.2 基础设施

- Node.js + npm（前端 `dist` 构建）
- Docker / Docker Compose
- MySQL 8.0+ client
- Redis CLI（监控 `queue:*` / `worker:heartbeat:*` / `alert_dedupe:*` / `task_lock:*`）
- curl / Postman
- 浏览器（前端 + Swagger UI）
- 企业微信 App 或 Mock server
- 备份磁盘空间 ≥ 50 GB
- `freezegun` / Python（TTL 时钟注入）

### 4.3 必备测试数据

- 管理员 `admin/admin123`（测试环境）
- 5 个真实企微用户（2 工人 + 2 厂家 + 1 中介）
- 岗位 ≥ 50 条（覆盖在线 / 已招满 / 主动下架 / 过期 / 待审 / 已驳回）
- 简历 ≥ 20 条（覆盖不同 TTL 剩余、性别、年龄）
- 对话日志 ≥ 200 条（覆盖 7 天分布）
- 审核日志 ≥ 50 条
- `wecom_inbound_event` ≥ 500 条（覆盖所有状态）
- `status='deleted'` 且 `extra.deleted_at` 已过 7 天的用户 ≥ 3 个
- `status='deleted'` 但 `extra.deleted_at` 缺失（历史遗留）的用户 ≥ 1 个

### 4.4 时钟模拟

- TTL / 硬删测试用 `freezegun` 或通过 `User.extra['deleted_at']` 预置时间
- 不允许直接改宿主机系统时间

## 5. 测试用例设计

### 5.1 调度进程（同进程）

#### TC-7.1.1 app 启动即调度就绪

- 操作：`docker compose up -d` 或本地 `uvicorn app.main:app`
- 预期：启动日志先出现 `scheduler pending: id=ttl_cleanup trigger=cron[...]` 等 7 行 pending 记录，`start()` 后再出现 `scheduler running: id=... next_run=2026-...` 7 行 running 记录；无独立 scheduler 容器；**不应出现 `AttributeError: 'Job' object has no attribute 'next_run_time'`**

#### TC-7.1.2 优雅关闭

- 操作：`docker stop jobbridge-app` 或 SIGTERM
- 预期：lifespan shutdown 调用 `scheduler.shutdown(wait=False)`；无异常堆栈

#### TC-7.1.3 任务内部异常不拖垮 app

- 操作：在某任务临时 `raise Exception`
- 预期：loguru 记录异常堆栈；FastAPI 继续服务请求；下一次定时仍触发

#### TC-7.1.4 横向扩容单实例锁（**本地双 uvicorn** 方案）

- **说明**：`docker-compose.prod.yml` 中 `app` 服务配置了 `container_name: jobbridge-app`，与 `docker compose --scale app=2` 冲突（Compose 不允许同名容器）。本测试不使用 compose scale。
- 前置：本地终端 A 启动 `uvicorn app.main:app --port 8001`；终端 B 启动 `uvicorn app.main:app --port 8002`；两者共享同一 Redis / MySQL
- 操作：同一时刻 cron 触发 `ttl_cleanup`（可人工调低频率或手工调用 run()）
- 预期：**只有一个实例**获得 `task_lock:ttl_cleanup`；另一个 loguru 记录 `ttl_cleanup: skipped, lock held`
- 备选：如团队确实想用 compose 验证，需运维侧先在 `docker-compose.prod.yml` 去掉 `app` 的 `container_name` 行，此决策不纳入 Phase 7 默认 checklist

#### TC-7.1.5 `max_instances=1 + coalesce=True` 生效

- 前置：人工延长任务耗时，使之超过 cron 间隔
- 预期：新触发被合并（coalesce），不堆叠

#### TC-7.1.6 task_lock owner token 正确性

- 自动化：pytest，使用短 TTL（如 2s）
- 步骤：
  1. 进程 A 取锁（TTL=2s）
  2. 进程 A 任务模拟运行 3s（超过 TTL）
  3. 进程 B 在 A 运行中重新取锁（2s 后应成功）
  4. 进程 A 结束后调用 release
- 预期：进程 A 的 release 通过 Lua CAS 发现 value 已变（不是自己的 token），**不删除** B 的锁；进程 B 仍然持有

#### TC-7.1.7 旧库启动自愈（`ensure_ttl_config_defaults`）

- 前置：从旧备份导入的 MySQL，`system_config` 中**不含** `ttl.audit_log.days` / `ttl.wecom_inbound_event.days` / `ttl.hard_delete.delay_days`
- 操作：启动 app 进程（触发 `scheduler.start()`）
- 预期：
  - 启动日志含 `ensure_ttl_config_defaults: inserted missing key 'ttl.audit_log.days'=180. Existing environment did NOT run phase7_001 migration.` 等 3 行 warning
  - 启动日志含 `scheduler: self-healed 3 missing ttl.* system_config key(s)`
  - `SELECT config_key FROM system_config WHERE config_key LIKE 'ttl.%'` 可见全部 6 个 key
  - 再次重启 app，不再打印 self-healed 日志（幂等）

#### TC-7.1.8 迁移 SQL 幂等性

- 前置：任意环境
- 操作：连续两次执行 `docker exec -i jobbridge-mysql mysql ... < phase7_001_ensure_system_config.sql`
- 预期：两次均无报错；末尾 SELECT 输出 6 行 `ttl.*` key；既有 key 的 value 保持不变（`INSERT IGNORE` 不覆盖）

#### TC-7.1.9 运营修改 TTL 配置后生效

- 前置：启动后 `system_config.ttl.hard_delete.delay_days = 7`
- 操作：通过 admin 配置页将其改为 3；第二天 03:00 触发 `ttl_cleanup`
- 预期：软删 4 天的数据被硬删（证明读取的是 DB 中的配置值，不是代码 hardcode 默认）

### 5.2 TTL 清理

#### TC-7.2.1 岗位过期软删

- 前置：`job.expires_at = NOW() - 1d`，`delist_reason=NULL`
- 操作：`ttl_cleanup.run()`
- 预期：`delist_reason='expired'` + `deleted_at=NOW()`；loguru `ttl_cleanup_summary.soft_delete_jobs >= 1`

#### TC-7.2.2 简历过期软删

- 类似 TC-7.2.1

#### TC-7.2.3 岗位 7 天硬删 + 附件清理

- 前置：`job.deleted_at = NOW() - 8d`，无附件（job 没有 storage，跳过附件）
- 预期：DB 中该 job 不可查

#### TC-7.2.4 简历 7 天硬删含敏感字段 + 附件

- 前置：`resume.deleted_at = NOW() - 8d`，含 `ethnicity` / `taboo` / `images=['a.jpg']`
- 预期：DB 中该 resume 不可查；`storage.delete('a.jpg')` 被调用

#### TC-7.2.5 用户主动删除 7 天硬删（extra.deleted_at 正常）

- 前置：`user.status='deleted'`，**`user.extra = {"deleted_at":"2026-04-10 00:00:00"}`**（MySQL 友好的 UTC 字符串，**无 `T`、无时区后缀**）；now = 2026-04-18
- 预期：该用户 `resume` / `conversation_log` 全部硬删；`user` 记录保留
- **注意**：若 `extra.deleted_at` 保存为 `"2026-04-10T00:00:00+00:00"` ISO8601 格式，SQL `STR_TO_DATE` 解析会返回 NULL 导致比较失败（用户永远被硬删或永远不被硬删，取决于 COALESCE 兜底）。请确认 `delete_user_data()` 存储格式统一为 `%Y-%m-%d %H:%M:%S`

#### TC-7.2.6 用户主动删除 7 天硬删（extra.deleted_at 缺失 → audit_log 兜底）

- 前置：`user.status='deleted'`，`user.extra` 为空；`audit_log` 中最近一条 `target_type='user', target_id=<userid>, action='auto_pass', operator='system', reason LIKE '%/删除我的信息%'` 的 `created_at = NOW() - 8d`
- 预期：硬删生效（兜底查询命中）

#### TC-7.2.7 对话日志 30 天硬删

#### TC-7.2.8 审核日志 180 天硬删

#### TC-7.2.9 `wecom_inbound_event` 30 天硬删

#### TC-7.2.10 分批执行

- 前置：构造 1200 条符合硬删条件
- 预期：分 3 批 `LIMIT 500`；每批独立 commit；loguru 总计 affected=1200

#### TC-7.2.11 某步失败继续

- 前置：mock 其中一张表硬删抛异常
- 预期：其它步骤继续执行并完成；loguru 中含 failed 汇总

#### TC-7.2.12 未过期数据不被误伤

- 前置：构造一批 `expires_at > now` 的在线岗位 / 简历
- 预期：这些数据无变更；DELETE 日志中无其主键

### 5.3 Worker / 队列监控

#### TC-7.3.1 Worker 正常心跳不告警

#### TC-7.3.2 Worker 全部离线 3 分钟内告警

- 预期：loguru `event="worker_all_offline"`；运营群收到 1 次推送

#### TC-7.3.3 多 Worker 部分离线不告警

- 前置：scale worker=2；停一个
- 预期：不告警（仍有活跃 heartbeat）

#### TC-7.3.4 队列积压告警

- 操作：`RPUSH queue:incoming` × 60
- 预期：1 分钟内 loguru + 推送

#### TC-7.3.5 死信告警

- 操作：`RPUSH queue:dead_letter "mock"`
- 预期：1 分钟内 loguru + 推送

#### TC-7.3.6 告警去重

- 操作：连续 3 分钟死信 > 0
- 预期：群消息 10 分钟窗口内只 1 次；loguru 每次都记

#### TC-7.3.7 阈值走 `.env`

- 操作：修改 `.env` 中 `MONITOR_QUEUE_INCOMING_THRESHOLD=10` 后 `docker compose up -d --no-deps app`
- 预期：下次巡检使用新阈值；**不通过 admin 页面修改**

#### TC-7.3.8 群消息重试队列积压

- 操作：`RPUSH queue:group_send_retry` × 30
- 预期：10 分钟内触发 `send_retry_backlog` 告警

### 5.4 出站补偿

#### TC-7.4.1 点对点重试（Phase 4 回归）

- 前置：mock `send_text` 连续失败 3 次
- 预期：`retry_count` 1→3，退避 60s/120s/300s；3 次后 loguru `send_failed_final`；**`queue:send_retry` 由 Phase 4 Worker 消费**

#### TC-7.4.2 群消息重试成功

- 前置：mock `send_text_to_group` 第 1 次失败，第 2 次成功
- 操作：入队 `queue:group_send_retry`
- 预期：30s 后首次 drain 失败，退避 60s；下一轮成功；loguru `group_send_retry_success`

#### TC-7.4.3 群消息重试 3 次失败

- 预期：loguru `event="group_send_failed_final"`；从队列中移除

#### TC-7.4.4 Phase 7 不消费 `queue:send_retry`

- 操作：人工入队 `queue:send_retry`
- 预期：**`send_retry_drain.py` 不处理它**（由 Phase 4 Worker 处理）

### 5.5 每日日报

#### TC-7.5.1 正常推送

- 前置：`DAILY_REPORT_CHAT_ID` 有效
- 预期：运营群收到日报；loguru `daily_report_generated`

#### TC-7.5.2 字段完整性

- 预期：日报含 DAU / 上传 / 检索 / 命中率 / 空召回率 / 打回率 / 封禁数 / 待审积压 / 死信 / 队列 / Worker 健康 / 昨日对比

#### TC-7.5.3 昨日对比箭头

- 前置：今日 DAU=432 昨日=420
- 预期：`432（↑ 12）`

#### TC-7.5.4 chat_id 未配置降级

- 前置：`.env` 中 `DAILY_REPORT_CHAT_ID=`（空）
- 预期：loguru `daily_report_skipped_no_chat_id`；任务成功结束

#### TC-7.5.5 推送失败进入 `queue:group_send_retry`

- 前置：mock `send_text_to_group` 返回 False
- 预期：`queue:group_send_retry` 中出现对应 payload；任务本身不抛错；loguru `daily_report_generated, pushed=False`

### 5.6 LLM 调用日志

#### TC-7.6.1 成功调用打点

- 预期：loguru `event="llm_call"` 含 provider / model / prompt_version / tokens / duration_ms / status="ok"

#### TC-7.6.2 JSON 解析失败

- 前置：mock LLM 返回非 JSON
- 预期：`status="parse_failed"`；`intent` 兜底 chitchat

#### TC-7.6.3 超时

- 预期：`status="timeout"`；1 次重试

#### TC-7.6.4 **不写 audit_log**

- 预期：`audit_log` 中无 `action='llm_call'`（不存在该枚举）

### 5.7 Docker 编排

#### TC-7.7.1 5 容器拉起

- 操作：`docker compose -f docker-compose.prod.yml up -d`
- 预期：**nginx / app / worker / mysql / redis** 五容器 `running` / `healthy`；**无 scheduler 容器**

#### TC-7.7.1A nginx 配置合法性

- 操作：`docker exec jobbridge-nginx nginx -t`
- 预期：`syntax is ok` + `test is successful`；无 warning

#### TC-7.7.2 健康检查

- 预期：`/health` 返回 `{"status":"ok"}`；`/nginx-health` 返回 `ok`

#### TC-7.7.3 重启恢复

- 操作：`docker restart jobbridge-app`
- 预期：30 秒内 healthy；scheduler 随 app 一起恢复；启动日志重新打印任务注册表

#### TC-7.7.4 数据持久化

- 操作：`docker compose down && up -d`
- 预期：MySQL 数据保留，Redis AOF 恢复，上传目录保留

#### TC-7.7.5 无独立 scheduler 容器

- 操作：`docker ps --format '{{.Names}}'`
- 预期：输出不含 `scheduler` 关键字

### 5.8 nginx `/admin/*` 分流

#### TC-7.8.1 axios JSON 请求走 API

- 操作：`curl -H "Accept: application/json" -X GET http://localhost/admin/me`
- 预期：返回 JSON（401 或 200 均可，证明走到 app）

#### TC-7.8.2 axios POST 请求走 API

- 操作：`curl -H "Accept: application/json" -H "Content-Type: application/json" -X POST http://localhost/admin/login -d '{"username":"admin","password":"admin123"}'`
- 预期：返回 JSON `{"code":0,...}` 或错误 JSON

#### TC-7.8.3 浏览器 GET `/admin/login` 返回 SPA

- 操作：`curl -X GET -H "Accept: text/html,*/*" http://localhost/admin/login`
- 预期：返回 `<!DOCTYPE html>` HTML

#### TC-7.8.3A 静态资源路径正确

- 前置：`frontend/vite.config.js` 已设 `base: '/admin/'` 并 build
- 操作：在浏览器打开 `http://localhost/admin/login`，查看 DevTools Network
- 预期：`/admin/assets/*.js` 与 `/admin/assets/*.css` 全部 200；**不出现 `/assets/*` 404**

#### TC-7.8.4 浏览器 GET `/admin/dashboard` 返回 SPA（路由 fallback）

- 操作：`curl -X GET -H "Accept: text/html,*/*" http://localhost/admin/dashboard`
- 预期：返回 `index.html` 内容（前端 router 处理）

#### TC-7.8.4A `/admin` 不带尾斜杠跳转

- 操作：`curl -I http://localhost/admin`
- 预期：302 重定向到 `/admin/`

#### TC-7.8.5 非 GET 方法无论 Accept 均走 API

- 操作：`curl -X PUT -H "Accept: text/html" http://localhost/admin/jobs/1`
- 预期：返回 JSON（401 / 400 均可，不是 HTML）

#### TC-7.8.6 `/webhook/*` 反代

- 操作：`curl -X POST http://localhost/webhook/wecom?...`
- 预期：到达 app（验签失败 403 或成功 200）

#### TC-7.8.7 `/api/events/*` 反代

- 操作：`curl -X POST http://localhost/api/events/miniprogram_click -H "X-API-Key: ..."`
- 预期：按 Phase 5 契约响应

#### TC-7.8.8 `/` 返回后端响应

- 操作：`curl http://localhost/`
- 预期：由 app 处理（取决于 app 是否暴露 `/`）

#### TC-7.8.9 gzip / body size

- 预期：响应头含 `Content-Encoding: gzip`；上传接近 10MB 图片不被 413 拦截

### 5.9 生产基线

#### TC-7.9.1 生产 `CORS_ORIGINS=*` 启动失败

- 前置：`APP_ENV=production` + `CORS_ORIGINS=*`
- 预期：`config.py` validator 抛 ValueError，app 启动退出

#### TC-7.9.1A 生产 `CORS_ORIGINS` 含 `*` 的多值启动失败

- 前置：`APP_ENV=production` + `CORS_ORIGINS=https://a.com,*`
- 预期：validator 逐个 origin 校验，命中 `*` 抛 ValueError 退出

#### TC-7.9.2 生产 `CORS_ORIGINS` 空启动失败

- 前置：`APP_ENV=production` + `CORS_ORIGINS=`（空）
- 预期：validator 抛 ValueError 退出

#### TC-7.9.3 合法单值 `CORS_ORIGINS` 正常启动

- 前置：`APP_ENV=production` + `CORS_ORIGINS=https://admin.example.com`
- 预期：app 正常启动；跨域 `OPTIONS` 响应含该域名

#### TC-7.9.3A 合法多值 `CORS_ORIGINS` 正常启动

- 前置：`APP_ENV=production` + `CORS_ORIGINS=https://a.com,https://b.com`
- 预期：app 正常启动；两个域名均可通过 CORS

#### TC-7.9.4 admin 默认密码替换

- 预期：生产上线前已 `UPDATE admin_user`，`admin123` 登录失败

### 5.10 备份与恢复

#### TC-7.10.1 备份脚本生成文件

#### TC-7.10.2 备份保留 14 天

#### TC-7.10.3 uploads 备份

#### TC-7.10.4 预发恢复演练

- 预期：预发 MySQL 数据导入成功；预发 app 启动后可登录、查询、页面无白屏

#### TC-7.10.5 Redis AOF 恢复

#### TC-7.10.6 备份脚本不泄密

- 预期：备份日志不含明文 `.env` 内容或 `admin_user.password_hash`

### 5.11 E2E 业务场景

按 `phase7-main.md §3.1 模块 L` 的 E1 ~ E13 执行。

#### TC-7.11.E1~E5

- 口径同 Phase 4 / 5，本阶段只做抽查回归

#### TC-7.11.E6 `/删除我的信息` 7 天闭环

- 操作：发送命令 → 检查 `user.extra.deleted_at` 存在；人工改 `extra.deleted_at = now-8d` → 跑一次 ttl_cleanup
- 预期：该用户 `resume` / `conversation_log` 全部硬删；`user` 记录保留；`status='deleted'`

#### TC-7.11.E7 岗位命令回归

#### TC-7.11.E8 Worker 高可用

- 预期：消息暂存 `queue:incoming`；Worker 重启后启动自检把 `processing` 消息重排

#### TC-7.11.E9 积压告警可见

- 预期：运营群收到推送；loguru 可见

#### TC-7.11.E10 运营后台审核链路

#### TC-7.11.E11 小程序点击回传（**条件必测**）

- **有客户埋点时**：
  - `POST /api/events/miniprogram_click` → 事件表落库 → 10 分钟同 userid + target_id 去重
  - 验收纳入"详情点击率"
- **无客户埋点时**：
  - E11 跳过
  - 验收改用"推荐后二次追问率"替代
  - §17.3 外部依赖确认单中"小程序埋点"标记 `有风险` + 备选方案

#### TC-7.11.E12 日报按时推送

#### TC-7.11.E13 TTL 任务按时执行

### 5.12 MVP 指标实测（7 天试运营）

| 指标 | 阈值 | 统计方式 |
|---|---|---|
| 结构化提取成功率 | ≥ 85% | `conversation_log` + LLM 调用日志（loguru） |
| 检索成功率 | ≥ 95% | 检索日志 |
| 空召回率 | ≤ 25% | 检索日志 |
| 审核打回率 | ≤ 15% | `audit_log.action IN ('manual_reject','auto_reject')` |
| 死信率 | ≤ 0.5% | `wecom_inbound_event.status='dead_letter'` / 总入站 |
| P95 回复延迟 | ≤ 5 秒 | 起点 `wecom_inbound_event.created_at`（实际字段名，**非 `received_at`**）；终点同一 `userid` 下首条 `conversation_log(direction='out', created_at >= in.created_at)` 的 `created_at`；依赖 Phase 4 `session_lock:{userid}` 保证同用户消息串行；**不使用 `wecom_msg_id` 关联**（出站日志的 `wecom_msg_id` 固定为 NULL）；误差 < 1s |
| 删除请求完成率 | = 100% | `User.extra['deleted_at']` 写入后 7 天扫描完成的比例 |

输出汇总表（"指标 / 目标 / 实测 / 状态 / 数据来源"），任一不达标列入遗留。

### 5.13 上线前 Checklist（§14.5.4）

- `.env` 所有 `change-me` 替换
- admin 默认密码替换
- 企微回调 URL 公网
- 防火墙只开 80 / 443
- MySQL 定时备份 cron
- `/health` 返回 ok

### 5.14 外部依赖确认（§17.3.3）

- 企微认证级别与出站 API 路径
- 企微回调公网地址与 HTTPS
- 隐私政策页链接
- LLM 供应商账号与额度
- 生产服务器与备份策略
- 小程序点击埋点（按 §17.1.3 降级）

### 5.15 Phase 1~6 回归

抽查：

- 登录 / 改密 / token 过期
- 审核工作台 lock / pass / reject / undo
- 岗位 / 简历 CRUD / 下架 / 延期
- 字典 / 系统配置 / 报表 / 对话日志
- 工人求职 / 厂家发岗 / 中介双向
- `/重新找` / `/删除我的信息` / `/帮助` / `/我的状态`

### 5.16 自动化建议

- TTL 清理：pytest + `freezegun` + `User.extra.deleted_at` 预置
- 监控任务：pytest + Redis fixture
- 日报：pytest + mock `WeComClient.send_text_to_group`
- nginx 分流：`scripts/phase7_nginx_matrix.sh`（curl 矩阵脚本）
- Docker：GitHub Actions 或本地脚本 `docker compose up -d` + curl 断言
- 指标：`scripts/phase7_indicator_snapshot.py` 每日跑

## 6. 验收确认项

- [ ] 调度在 app 进程内按 cron 触发；横向扩容下分布式锁有效
- [ ] TTL 软删 / 硬删按口径执行，未过期数据不被误伤
- [ ] `/删除我的信息` → `User.extra['deleted_at']` → 7 天硬删闭环可观测
- [ ] Worker 心跳 / 队列 / 死信 / 出站补偿告警按预期触发且 10 分钟去重；运维事件走 loguru，不写 audit_log
- [ ] 每日日报按时推送；失败进入 `queue:group_send_retry` 并被消费
- [ ] LLM 调用日志可按维度检索
- [ ] Docker **5 容器**一键拉起；`/health` / `/nginx-health` 正常
- [ ] nginx `/admin/*` 对 axios JSON 和浏览器 HTML 正确分流
- [ ] `CORS_ORIGINS` 生产校验生效
- [ ] 备份脚本每日生效 + 预发恢复演练完成
- [ ] §17.1.4 七项 MVP 指标在 §17.1.1 要求的至少 7 天试运营后达标（P95 按新口径）
- [ ] E11 按降级规则执行
- [ ] §14.5.4 上线前 Checklist 全部勾选
- [ ] §17.3.3 5 项必关闭项全部 `已确认`
- [ ] E2E E1~E13 全部通过
- [ ] Phase 1~6 回归抽查未见退化
- [ ] `phase7-release-report.md` 已归档

## 7. 缺陷上报模板

每个缺陷至少包含：

- 测试用例编号（如 `TC-7.2.5`）
- 所属模块（scheduler / tasks / worker_monitor / docker / nginx / CORS / backup / E2E / indicator）
- 环境（dev / staging / prod）
- 复现步骤
- 期望结果
- 实际结果
- 关键日志（loguru 结构化行 / `docker logs` / audit_log）
- 受影响的阶段（Phase 4 / 5 / 6 / 7）
- 是否阻塞上线（是 / 否）

## 8. 注意事项

- 指标实测必须是真实业务数据，禁止临时改规则
- 备份恢复演练必须在预发完成一次；仅跑 `backup_mysql.sh` 不算演练
- 告警去重窗口以 `.env` 的 `MONITOR_ALERT_DEDUPE_SECONDS` 为准
- TTL 测试必须用 `freezegun` 或预置 `extra.deleted_at`，禁止改宿主机时间
- nginx 分流测试必须覆盖"Accept: application/json"、"Accept: text/html"、"Accept: */*"、"无 Accept"四种请求
- 生产首次上线前冒烟必须在真实域名 + HTTPS + 真实企微账号下完成
- 发现 Phase 1~6 遗留缺陷，优先判断是否阻塞上线；非阻塞项统一进 Backlog
