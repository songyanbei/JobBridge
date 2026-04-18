# Phase 7 开发 Checklist

> 基于：`collaboration/features/phase7-main.md`
> 配套实施文档：`collaboration/features/phase7-dev-implementation.md`
> 面向角色：后端开发 + DevOps
> 状态：`draft`
> 创建日期：2026-04-18
> 最近修订：2026-04-18（按 codex 评审反馈修订）

## A. 开发前确认

- [ ] 已阅读 `collaboration/features/phase7-main.md`（含 §0 本版修订说明）
- [ ] 已阅读 `collaboration/features/phase7-dev-implementation.md`
- [ ] 已阅读 `docs/implementation-plan.md` §4.8
- [ ] 已阅读 `方案设计_v0.1.md` §7.7 / §12.5 / §12.6 / §13.3 / §13.5 / §14.5 / §17.1.1 / §17.1.3 / §17.1.4 / §17.2.4 / §17.3
- [ ] 已阅读 `docs/architecture.md` §二 / §三 / §五 / §七.4
- [ ] 已确认 Phase 4 / 5 / 6 均已验收
- [ ] 已确认 Phase 7 **不新增业务功能**
- [ ] 已确认 Phase 7 **不扩展 `audit_log.action` 枚举**、**不新增 `audit_log.extra` 列**
- [ ] 已确认 Phase 7 **不新增独立 scheduler 容器 / 进程**
- [ ] 已确认 `queue:send_retry` 是 Phase 4 点对点通道；群消息走 `queue:group_send_retry`
- [ ] 已确认 7 天计时以 `User.extra['deleted_at']` 为准
- [ ] 已确认生产 `CORS_ORIGINS` 不得为 `*` 或空
- [ ] 已确认企微回调域名 / HTTPS 证书就绪
- [ ] 已确认 LLM API Key 可用并有额度
- [ ] 已确认备份目录空间

## B. 调度进程骨架（同进程）

- [ ] `backend/app/tasks/common.py` 已创建（task_lock + log_event）
- [ ] `task_lock` 使用随机 owner token（`secrets.token_hex(16)`）作为锁值
- [ ] `task_lock` 通过 Lua CAS 脚本释放锁，避免任务超时后误删他人锁
- [ ] `task_lock` 的 ttl 设置 ≥ 任务预期最大耗时 × 2
- [ ] `backend/app/tasks/scheduler.py` 已创建，暴露 `start()` / `shutdown()`
- [ ] 使用 `BackgroundScheduler`（**非 BlockingScheduler**）
- [ ] 时区 `Asia/Shanghai`（来自 `settings.scheduler_timezone`）
- [ ] `max_instances=1 + coalesce=True`
- [ ] 任务入口用 `task_lock(task_name, ttl=...)` 分布式锁
- [ ] 未获得锁时只打 loguru，不抛异常
- [ ] `app/main.py` lifespan 启动 / 关闭 APScheduler
- [ ] **启动日志先打印 `scheduler pending: id=... trigger=...`，调用 `scheduler.start()` 后再打印 `scheduler running: id=... next_run=...`**（pending job 在 start 前没有 `next_run_time`，直接访问会 `AttributeError`）
- [ ] **不创建 scheduler Docker service**
- [ ] `scheduler.py` 不含业务 SQL、不 import `api/*`

## C. TTL 清理

- [ ] `backend/app/tasks/ttl_cleanup.py` 已创建
- [ ] 每日 03:00 执行
- [ ] 岗位过期软删 `delist_reason='expired' + deleted_at=NOW()`
- [ ] 简历过期软删 `deleted_at=NOW()`
- [ ] `job.deleted_at < NOW() - 7d` 硬删
- [ ] `resume.deleted_at < NOW() - 7d` 硬删 + `storage.delete()`
- [ ] 用户主动删除硬删：`user.status='deleted' AND JSON_EXTRACT(user.extra,'$.deleted_at') < NOW() - 7d`（历史遗留走 audit_log 兜底）
- [ ] `conversation_log.created_at < NOW() - 30d` 硬删
- [ ] `wecom_inbound_event.created_at < NOW() - 30d` 硬删
- [ ] `audit_log.created_at < NOW() - 180d` 硬删
- [ ] 每步分批 `LIMIT 500`
- [ ] 每步独立 commit
- [ ] 每步写 loguru 汇总（`log_event("ttl_cleanup_summary", ...)`）
- [ ] **不写 `audit_log`**
- [ ] 某步失败不影响其它步骤
- [ ] 未过期数据未被误伤

## D. `User.extra['deleted_at']` 写入

- [ ] `services/user_service.delete_user_data()` 末尾写入 `User.extra['deleted_at'] = now.strftime("%Y-%m-%d %H:%M:%S")`（**不用 `isoformat()`**）
- [ ] 存储内容为 UTC，无 `T` 无时区后缀
- [ ] 复用现有 `now = datetime.now(timezone.utc)`
- [ ] `User.extra` 为 `MutableDict.as_mutable(sa.JSON)`，赋值新 dict 触发更新
- [ ] ttl_cleanup 7 天扫描 SQL 使用 `STR_TO_DATE(..., '%Y-%m-%d %H:%i:%s') < UTC_TIMESTAMP() - INTERVAL 7 DAY`
- [ ] 历史遗留走 audit_log 兜底分支
- [ ] MySQL 容器时区配置为 UTC（或明确服务器时区并在 SQL 中 `CONVERT_TZ`）
- [ ] （可选）`scripts/backfill_user_deleted_at.py` 一次性补历史数据，按 `%Y-%m-%d %H:%M:%S` 格式

## E. Worker / 队列监控

- [ ] `tasks/worker_monitor.py` 已创建
- [ ] `check_heartbeat` 180s 运行
- [ ] `check_queue_backlog` 60s 运行
- [ ] `check_dead_letter` 60s 运行
- [ ] 阈值走 `.env`（`MONITOR_QUEUE_INCOMING_THRESHOLD` 等），**不进 admin UI**
- [ ] 运维事件写 loguru `logger.bind(event=...)`，**不写 audit_log**
- [ ] 首次告警推送运营群；10 分钟内不重复（`alert_dedupe:{event}` SETNX EX 600）
- [ ] 心跳检查支持多 Worker：有任一活跃 heartbeat 不告警
- [ ] 死信 > 0 立刻首次推送

## F. 出站补偿与群消息重试

- [ ] `tasks/send_retry_drain.py` 已创建
- [ ] `check_backlog` 每 10 分钟扫 `queue:send_retry` + `queue:group_send_retry` 两个队列
- [ ] `drain_group_send_retry` 每 30 秒消费 `queue:group_send_retry`
- [ ] **不消费 `queue:send_retry`**（Phase 4 Worker 的职责）
- [ ] 群消息 payload 字段：`chat_id / content / retry_count / backoff_until`
- [ ] 指数退避 60s / 120s / 300s
- [ ] 3 次失败后 `log_event("group_send_failed_final", ...)` 丢弃

## G. 日报

- [ ] `tasks/daily_report.py` 已创建
- [ ] 每日 09:00 执行
- [ ] 数据来源走 `services/report_service.py`
- [ ] 内容含：DAU / 上传 / 检索 / 命中率 / 空召回率 / 打回率 / 新增封禁 / 待审积压 / 队列 / 死信 / Worker 健康 / 昨日对比
- [ ] 昨日对比箭头（↑ ↓ ±）渲染正确
- [ ] `DAILY_REPORT_CHAT_ID` 为空时只打 loguru，不报错
- [ ] 推送失败写 `queue:group_send_retry`
- [ ] **不写 `audit_log`**

## H. `WeComClient` 扩展

- [ ] `send_text_to_group(chat_id, content) -> bool` 已实现
- [ ] 调用 `cgi-bin/appchat/send`
- [ ] errcode=0 返回 True，其它返回 False（不抛异常）
- [ ] token 过期自愈逻辑仍走现有 `get_token()` 通道
- [ ] `enqueue_group_send_retry(chat_id, content, ...)` 已实现

## I. LLM 调用日志

- [ ] `intent_service.classify_intent()` 调用路径打点
- [ ] `search_service.rerank()` 调用路径打点
- [ ] 打点字段：provider / model / prompt_version / input_tokens / output_tokens / duration_ms / intent / user_msg_id / status(`ok|parse_failed|timeout|http_error`)
- [ ] 通过 `log_event("llm_call", ...)` 写 loguru JSON
- [ ] **不写 audit_log**
- [ ] 不重复写 `raw_response`

## J. Docker 编排

- [ ] `docker-compose.yml` / `docker-compose.prod.yml` **保持 5 容器**
- [ ] `docker compose -f docker-compose.prod.yml up -d` 全部 healthy
- [ ] scheduler 随 app 容器生命周期
- [ ] app 重启后任务自动恢复调度
- [ ] `docker ps` 无 restart loop
- [ ] 多 app 实例同时运行时，定时任务只在一个实例触发（分布式锁）
- [ ] **横向扩容分布式锁测试采用"本地启两个 uvicorn 实例"而不是 `docker compose --scale app=2`**（因为 `docker-compose.prod.yml` 中 `app` 服务有 `container_name: jobbridge-app`，scale 会报错；如确需 compose 验证，需先移除 `container_name`，作为运维侧决策项）

## K. nginx 反代（SPA / API 冲突修复 + 静态资源）

- [ ] `frontend/vite.config.js` 已设置 `base: '/admin/'`
- [ ] `frontend/dist/` 已重新 build；`index.html` 中 `<script>` / `<link>` 路径前缀为 `/admin/assets/...`
- [ ] `nginx/nginx.conf` 采用 `map $http_accept` + `if {return 418}` + `error_page 418 = @admin_api` + `recursive_error_pages on`
- [ ] `if` 块内**仅使用 `return`**；**所有 `proxy_pass` / `proxy_set_header`** 放在 `@admin_api` named location
- [ ] `location /admin/` 使用 `root /usr/share/nginx/html` + `try_files $uri $uri/ /admin/index.html`
- [ ] `location = /admin { return 302 /admin/; }`
- [ ] `location /` 代理其它后端路径（`/webhook/*` / `/api/*` / `/health` 等）
- [ ] `location = /nginx-health` 返回 200
- [ ] `client_max_body_size 10m`
- [ ] `gzip on`
- [ ] `proxy_read_timeout` 对 `@admin_api` 为 60s，对 `/` 为 30s
- [ ] **容器内 `nginx -t` 通过**
- [ ] 浏览器 DevTools 打开 `/admin/login`，Network 面板 `/admin/assets/*.js` / `*.css` 全部 200
- [ ] （生产）HTTPS 证书配置 `listen 443 ssl;` 等

## L. 生产基线

- [ ] `.env.example` 补 `APP_ENV` / `CORS_ORIGINS` / `SCHEDULER_TIMEZONE` / `DAILY_REPORT_CHAT_ID` / `MONITOR_*`
- [ ] `config.py` 在 `APP_ENV=production` 且 `CORS_ORIGINS=*` 启动报错退出
- [ ] `config.py` 在 `APP_ENV=production` 且 `CORS_ORIGINS` 为空启动报错退出
- [ ] `config.py` 在 `APP_ENV=production` 且 `CORS_ORIGINS=https://a.com,*` 启动报错退出（逐个 origin 校验）
- [ ] 合法单值 `CORS_ORIGINS=https://admin.example.com` 正常启动
- [ ] 合法多值 `CORS_ORIGINS=https://a.com,https://b.com` 正常启动
- [ ] admin 默认密码替换计划已准备（上线前 `UPDATE admin_user`）
- [ ] 企微 / LLM 敏感配置位于 `.env` 且未提交到 git
- [ ] MySQL / Redis 端口未对外暴露

## M. 备份与恢复

- [ ] `scripts/backup_mysql.sh` 生成 `.sql.gz`，保留 14 天
- [ ] `scripts/backup_uploads.sh` 生成 `tar.gz`，保留 14 天
- [ ] crontab 每日 03:30 / 04:00 已配置
- [ ] Redis AOF 启用
- [ ] `scripts/restore_drill.sh` 含"禁止在生产执行"注释
- [ ] 预发环境完成一次完整恢复演练
- [ ] 演练记录写入 `phase7-release-report.md`
- [ ] 备份脚本不向日志输出密码

## N. `system_config` 与 `.env` 新增项

**`system_config` 新增 key**（仅扩展 TTL 相关，沿用现有命名）：

- [ ] `ttl.audit_log.days` (默认 180)
- [ ] `ttl.wecom_inbound_event.days` (默认 30)
- [ ] `ttl.hard_delete.delay_days` (默认 7)
- [ ] 全部在 `backend/sql/seed.sql` 已补
- [ ] 不新增 `monitor.*` key 到 `system_config`

**`.env` 新增变量**：

- [ ] `APP_ENV`
- [ ] `CORS_ORIGINS`
- [ ] `SCHEDULER_TIMEZONE`
- [ ] `DAILY_REPORT_CHAT_ID`
- [ ] `MONITOR_QUEUE_INCOMING_THRESHOLD`
- [ ] `MONITOR_SEND_RETRY_THRESHOLD`
- [ ] `MONITOR_ALERT_DEDUPE_SECONDS`

## O. 端到端自测

- [ ] 手工触发 `ttl_cleanup.run()` 干跑通过
- [ ] 手工触发 `daily_report.run()` 推送到测试群
- [ ] 杀掉 worker → 3 分钟内运营群收到心跳告警
- [ ] `RPUSH queue:dead_letter "mock"` → 1 分钟内收到死信告警
- [ ] 批量灌入 `queue:incoming > 50` → 1 分钟内收到积压告警
- [ ] 连续触发同一告警 3 次 → 10 分钟内只推送 1 次
- [ ] `/删除我的信息` → `user.extra.deleted_at` 存在
- [ ] 模拟 8 天后再跑 ttl_cleanup → 残留硬删
- [ ] `docker compose up -d` 一键拉起 5 容器
- [ ] `/health` 返回 ok
- [ ] `curl -X GET -H "Accept: application/json" http://localhost/admin/me` 返回 JSON
- [ ] 浏览器访问 `http://localhost/admin/login` 返回 HTML
- [ ] 前端通过 nginx 可登录并完成审核 / 导出 / 查看日志
- [ ] 真实企微消息 P95 延迟 < 5 秒（口径见 dev-implementation §5.14）

## P. 上线前 Checklist（对齐 §14.5.4）

- [ ] `.env` 中所有 `change-me` 已替换
- [ ] admin 默认密码已替换
- [ ] 企微回调 URL 已配置公网域名
- [ ] 防火墙仅开放 80 / 443
- [ ] MySQL 定时备份 cron 已配置
- [ ] `/health` 端点返回 `{"status":"ok"}`
- [ ] nginx HTTPS 证书已配置（或说明临时 HTTP）

## Q. 外部依赖必关闭项（对齐 §17.3.3）

- [ ] 企微认证级别与出站 API 路径 `已确认`
- [ ] 企微回调公网地址与 HTTPS `已确认`
- [ ] 隐私政策页链接 `已确认`
- [ ] LLM 供应商账号与额度 `已确认`
- [ ] 生产服务器与备份策略 `已确认`
- [ ] **小程序点击埋点**：`已确认` / `有风险`（按 §17.1.3 降级执行）

## R. 指标实测与试运营

- [ ] 生成 §17.1.1 要求的 7 天试运营数据采集脚本或 SQL
- [ ] 每日输出 §17.1.4 七项指标
- [ ] P95 延迟口径按 dev-implementation §5.14（in_at→out_at）
- [ ] E11 按降级规则执行（有埋点用"详情点击率"，无埋点用"推荐后二次追问率"）
- [ ] 未达标项已列入遗留问题清单

## S. 验收报告与交付

- [ ] `collaboration/handoffs/phase7-release-report.md` 已创建
- [ ] 报告含：指标实测表、E2E 场景通过表、已知问题、遗留二期 Backlog
- [ ] 报告含：上线前 Checklist 执行记录
- [ ] 报告含：外部依赖确认单副本（含 E11 降级记录）
- [ ] 报告由技术 + 运营双方审阅
- [ ] `MEMBERS / 负责人 / 日期` 已签字
- [ ] PR / commit 已合并到 main

## T. 遗留与不做

- [ ] 所有新增需求已进入二期 Backlog
- [ ] Phase 6 未关闭 P0 缺陷已列入 `handoffs/frontend-to-backend.md`
- [ ] Phase 4 / 5 未解决 open handoff 已转归档或重新排期
- [ ] 未纳入本阶段的监控增强（Prometheus、APM、告警规则配置）已明确列为二期
- [ ] 未纳入本阶段的 `wecom_inbound_event.send_*_at` 字段增强已列为二期
