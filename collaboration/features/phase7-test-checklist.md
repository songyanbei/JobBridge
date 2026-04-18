# Phase 7 测试 Checklist

> 基于：`collaboration/features/phase7-main.md`
> 配套实施文档：`collaboration/features/phase7-test-implementation.md`
> 面向角色：测试 + 运维协同
> 状态：`draft`
> 创建日期：2026-04-18
> 最近修订：2026-04-18（按 codex 评审反馈修订）

## A. 测试前确认

- [ ] 已阅读 `collaboration/features/phase7-main.md`（含 §0 本版修订说明）
- [ ] 已阅读 `collaboration/features/phase7-test-implementation.md`
- [ ] 已阅读 `docs/implementation-plan.md` §4.8
- [ ] 已阅读 `方案设计_v0.1.md` §7.7 / §12 / §13.3 / §13.5 / §14.5 / §17.1.1 / §17.1.3 / §17.1.4 / §17.2 / §17.3
- [ ] 已确认 Phase 4 / 5 / 6 均已验收
- [ ] 已确认测试范围：定时任务（**同进程**）/ 监控 / Docker（**5 容器**）/ 备份 / E2E / 指标 / 上线 Checklist
- [ ] 已确认不测 Phase 1~6 内部实现
- [ ] 已确认 E11 按 §17.1.3 降级规则执行
- [ ] 已确认运维事件走 loguru，**不写 `audit_log`**
- [ ] 已确认 7 天计时起点以 `User.extra['deleted_at']` 为准
- [ ] 已确认群消息走 `queue:group_send_retry`，不污染 `queue:send_retry`
- [ ] 已确认监控阈值走 `.env`，不走 admin 配置页
- [ ] 测试数据就绪（管理员 / 企微成员 / 岗位 / 简历 / 对话 / inbound_event / deleted user 样本）
- [ ] 预发环境可用于恢复演练
- [ ] 企微真实账号或 Mock 降级策略
- [ ] LLM API 账号与额度可覆盖 7 天试运营

## B. 调度进程（同进程）

- [ ] app 启动日志**先**出现 `scheduler pending: id=... trigger=...` 7 行
- [ ] app 启动日志**后**出现 `scheduler running: id=... next_run=...` 7 行
- [ ] 启动过程**未出现** `AttributeError: 'Job' object has no attribute 'next_run_time'`
- [ ] **不存在独立 scheduler 容器**
- [ ] `BackgroundScheduler` 而非 `BlockingScheduler`
- [ ] 时区 `Asia/Shanghai`
- [ ] lifespan shutdown 调用 `scheduler.shutdown(wait=False)`
- [ ] 任务内部异常被捕获，不影响 FastAPI 主线程
- [ ] `task_lock:{task_name}` 分布式锁生效（owner token + Lua CAS 释放）
- [ ] `task_lock` 在任务超时后不误删他人锁（TC-7.1.6）
- [ ] **本地双 uvicorn 实例**测试：同一任务只被一个实例执行（不使用 `docker compose --scale app=2`，避免与 `container_name: jobbridge-app` 冲突）
- [ ] `max_instances=1 + coalesce=True` 生效

## C. TTL 清理

- [ ] 每日 03:00 触发
- [ ] 岗位过期软删：`delist_reason='expired' + deleted_at`
- [ ] 简历过期软删：`deleted_at`
- [ ] 岗位 7 天硬删
- [ ] 简历 7 天硬删 + `storage.delete()`
- [ ] 用户主动删除硬删（`extra.deleted_at` 命中）
- [ ] 用户主动删除硬删（`extra.deleted_at` 缺失 → audit_log 兜底）
- [ ] 对话日志 30 天硬删
- [ ] `wecom_inbound_event` 30 天硬删
- [ ] 审核日志 180 天硬删
- [ ] 分批 `LIMIT 500`
- [ ] 每步独立 commit
- [ ] 每步写 loguru 汇总
- [ ] **未写 audit_log**
- [ ] 某步失败其它步骤继续
- [ ] 未过期数据未被误伤

## D. `/删除我的信息` 闭环

- [ ] 命令触发后 `user.extra.deleted_at` 被写入 **UTC `%Y-%m-%d %H:%M:%S` 字符串**（不是 `isoformat()`）
- [ ] 字符串**无 `T`、无时区后缀**
- [ ] `user.status='deleted'`
- [ ] `resume.deleted_at` 立即被设置
- [ ] `conversation_log.expires_at` 立即被设置为当前时间
- [ ] 7 天后 ttl_cleanup 扫描命中（SQL 使用 `STR_TO_DATE(..., '%Y-%m-%d %H:%i:%s') < UTC_TIMESTAMP() - INTERVAL 7 DAY`）
- [ ] 硬删后 `resume` / `conversation_log` 不可查
- [ ] `user` 记录保留（防重复注册）
- [ ] MySQL 容器时区已配置为 UTC（或兜底分支含 `CONVERT_TZ`）

## E. 监控与告警

- [ ] Worker 正常心跳不告警
- [ ] Worker 全部离线 3 分钟内告警
- [ ] 多 Worker 部分离线不告警
- [ ] 队列 `queue:incoming` 超阈值 1 分钟内告警
- [ ] 死信 `queue:dead_letter > 0` 1 分钟内告警
- [ ] `queue:send_retry` 超阈值 10 分钟内告警
- [ ] `queue:group_send_retry` 超阈值 10 分钟内告警
- [ ] 同类告警 10 分钟内不重复推送
- [ ] loguru 每次巡检都记录
- [ ] 阈值改 `.env` 后 app 重启生效
- [ ] **不存在通过 admin 配置页修改阈值的入口**
- [ ] 推送失败不阻塞任务

## F. 出站补偿

### F.1 点对点（Phase 4 回归）

- [ ] `queue:send_retry` 由 Phase 4 Worker 消费
- [ ] 重试节奏 60s / 120s / 300s
- [ ] 重试 3 次失败 loguru `send_failed_final`
- [ ] token 过期自动刷新
- [ ] 用户不存在 / 离职停止重试

### F.2 群消息（Phase 7 新增）

- [ ] `queue:group_send_retry` 由 `tasks/send_retry_drain.drain_group_send_retry` 消费
- [ ] payload 字段：`chat_id / content / retry_count / backoff_until`
- [ ] 成功：loguru `group_send_retry_success`
- [ ] 3 次失败：loguru `group_send_failed_final`，丢弃
- [ ] Phase 7 任务**不消费 `queue:send_retry`**

## G. 每日日报

- [ ] 09:00 推送到运营群
- [ ] 字段：DAU / 上传 / 检索 / 命中率 / 空召回率 / 打回率 / 封禁 / 待审积压 / 死信 / 队列 / Worker
- [ ] 昨日对比箭头
- [ ] `DAILY_REPORT_CHAT_ID` 空时只打 loguru 不报错
- [ ] 推送失败写 `queue:group_send_retry`
- [ ] **未写 audit_log**

## H. LLM 调用日志

- [ ] 成功调用 loguru 含 provider / model / prompt_version / tokens / duration_ms
- [ ] JSON 解析失败可识别
- [ ] 超时可识别
- [ ] **不写 audit_log**
- [ ] 不重复写 `raw_response`

## I. Docker 编排

- [ ] `docker compose -f docker-compose.prod.yml up -d` **5 容器**全部 healthy
- [ ] 包含：nginx / app / worker / mysql / redis
- [ ] **不包含**：scheduler
- [ ] `docker exec jobbridge-nginx nginx -t` 通过
- [ ] `restart: unless-stopped` 生效
- [ ] 重启 app 后 scheduler 自动恢复
- [ ] `docker compose down && up -d` 数据保留
- [ ] 多 app 实例（**本地双 uvicorn**，不用 compose scale）调度不重复

## J. nginx `/admin/*` 分流与静态资源

- [ ] `frontend/vite.config.js` 已设置 `base: '/admin/'`
- [ ] `frontend/dist/index.html` 中 script/link 路径前缀为 `/admin/assets/`
- [ ] `docker exec jobbridge-nginx nginx -t` 通过
- [ ] `curl -X GET -H "Accept: application/json" http://localhost/admin/me` 返回 JSON
- [ ] `curl -X POST -H "Accept: application/json" -H "Content-Type: application/json" http://localhost/admin/login -d '{"username":"admin","password":"admin123"}'` 返回 JSON
- [ ] `curl -X GET -H "Accept: text/html" http://localhost/admin/login` 返回 HTML
- [ ] `curl -X GET -H "Accept: text/html" http://localhost/admin/dashboard` 返回 `index.html`
- [ ] `curl -I GET /admin` 返回 302 → `/admin/`
- [ ] 浏览器打开 `/admin/login`，DevTools 中 `/admin/assets/*.js` / `*.css` 全部 200
- [ ] `PUT /admin/jobs/1`（任意 Accept）返回 JSON
- [ ] `Accept: */*` 默认走 SPA（非 API）；axios 请求需显式 `application/json`
- [ ] `/webhook/*` 代理正常
- [ ] `/api/events/*` 代理正常
- [ ] `/health` 返回 `{"status":"ok"}`
- [ ] `/nginx-health` 返回 `ok`
- [ ] `client_max_body_size ≥ 10m`
- [ ] `gzip` 启用
- [ ] HTTPS 证书（若已有）

## K. 生产基线

- [ ] `APP_ENV=production` + `CORS_ORIGINS=*` 启动报错退出
- [ ] `APP_ENV=production` + `CORS_ORIGINS=https://a.com,*` 启动报错退出（逐个 origin 校验）
- [ ] `APP_ENV=production` + `CORS_ORIGINS=` 启动报错退出
- [ ] 合法 `CORS_ORIGINS` 正常启动
- [ ] 浏览器跨域 `OPTIONS` 响应含生产域名
- [ ] admin 默认密码已替换
- [ ] 企微 / LLM 敏感配置在 `.env` 且不入 git
- [ ] MySQL / Redis 端口未对外暴露

## L. 备份与恢复

- [ ] `scripts/backup_mysql.sh` 生成 `.sql.gz`
- [ ] 14 天以上备份被清理
- [ ] `scripts/backup_uploads.sh` 生成 `tar.gz`
- [ ] crontab 每日 03:30 / 04:00 可见
- [ ] Redis AOF 启用
- [ ] `restore_drill.sh` 含"禁止在生产执行"注释
- [ ] 预发 `restore_drill.sh` 成功
- [ ] 预发 **app（含同进程 scheduler）+ worker** 均可启动
- [ ] 演练记录写入 `phase7-release-report.md`
- [ ] 备份脚本不向日志输出密码

## M. E2E 业务场景

- [ ] E1 新工人首次消息自动注册 + 欢迎语
- [ ] E2 工人求职 Top 3 + 更多
- [ ] E3 厂家发布岗位自动通过 + 工人可检索
- [ ] E4 敏感词驳回链路
- [ ] E5 中介双向切换
- [ ] E6 `/删除我的信息` + `user.extra.deleted_at` + 7 天硬删
- [ ] E7 `/下架` `/招满了` `/续期`
- [ ] E8 Worker 高可用（kill + 启动自检）
- [ ] E9 队列积压告警运营群可见
- [ ] E10 运营后台审核 pass / reject / edit / undo 全流程
- [ ] **E11**（条件必测）：
  - [ ] 有埋点：小程序点击回传 → 事件表 → 10 分钟去重 → 纳入"详情点击率"
  - [ ] 无埋点：E11 跳过 → 使用"推荐后二次追问率"替代
- [ ] E12 每日 09:00 运营群日报
- [ ] E13 每日 03:00 TTL 任务未误伤

## N. MVP 指标实测（至少 7 天试运营，对齐 §17.1.1）

- [ ] 结构化提取成功率 ≥ 85%
- [ ] 检索成功率 ≥ 95%
- [ ] 空召回率 ≤ 25%
- [ ] 人工审核打回率 ≤ 15%
- [ ] 死信率 ≤ 0.5%
- [ ] **P95 端到端回复延迟 ≤ 5 秒**（口径：`wecom_inbound_event.created_at`（**非 `received_at`**）→ 同一 userid 下首条 `conversation_log(direction='out', created_at >= in.created_at)` 的 `created_at`；**不使用 `wecom_msg_id` 关联**，出站日志该字段固定为 NULL）
- [ ] `/删除我的信息` 完成率 = 100%
- [ ] 指标汇总表已生成（指标 / 目标 / 实测 / 状态 / 数据来源）
- [ ] E11 按降级规则输出对应替代指标值
- [ ] 未达标项已列入遗留问题清单

## O. 上线前 Checklist（§14.5.4）

- [ ] `.env` 所有 `change-me` 已替换
- [ ] admin 默认密码已替换
- [ ] 企微回调 URL 配置公网
- [ ] 防火墙仅开放 80 / 443
- [ ] MySQL 定时备份 cron 已生效
- [ ] `/health` 返回 ok
- [ ] HTTPS 证书有效（若已有）

## P. 外部依赖必关闭项（§17.3.3）

- [ ] 企微认证级别与出站 API 路径 `已确认`
- [ ] 企微回调公网地址与 HTTPS `已确认`
- [ ] 隐私政策页链接 `已确认`
- [ ] LLM 供应商账号与额度 `已确认`
- [ ] 生产服务器与备份策略 `已确认`
- [ ] 小程序点击埋点 `已确认` / 标记 `有风险` + 降级方案

## Q. Phase 1~6 关键路径回归

- [ ] 登录 / 改密 / token 过期跳转
- [ ] 审核工作台 lock / 续锁 / pass / reject / edit / undo / unlock
- [ ] 岗位 CRUD / 下架 / 招满 / 延期
- [ ] 简历 CRUD / 下架 / 延期（无 restore）
- [ ] 账号管理（厂家 / 中介 / 工人 / 黑名单）
- [ ] 字典（城市 / 工种 / 敏感词）
- [ ] 系统配置分组与保存
- [ ] 数据看板 trends / top / funnel / export
- [ ] 对话日志查询与导出
- [ ] 工人求职 / 厂家发岗 / 中介双向
- [ ] `/帮助` / `/重新找` / `/找岗位` / `/找工人` / `/续期` / `/下架` / `/招满了` / `/删除我的信息` / `/人工客服` / `/我的状态`

## R. 验收交付

- [ ] 测试用例执行记录完整
- [ ] 缺陷清单按阻塞 / 非阻塞分类
- [ ] 阻塞项已修复并回归通过
- [ ] 非阻塞项已进入二期 Backlog
- [ ] MVP 指标汇总表已交付
- [ ] 上线 Checklist 执行记录已交付
- [ ] 外部依赖确认单（含 E11 降级）已交付
- [ ] `collaboration/handoffs/phase7-release-report.md` 已归档
- [ ] 技术 + 运营签字确认
