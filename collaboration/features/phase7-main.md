# Feature: Phase 7 定时任务、联调与上线验收

> 状态：`draft`
> 创建日期：2026-04-18
> 最近修订：2026-04-18（按 codex 评审修订：scheduler 改为同进程、audit_log 改走 loguru、user 删除时间使用 extra、nginx SPA/API 冲突修正、群日报独立重试队列、P95 口径、config key 命名、文档事实性修正）
> 对应实施阶段：Phase 7
> 关联实施文档：`docs/implementation-plan.md` §4.8
> 关联方案设计章节：§3（非功能性需求）、§7.7（数据生命周期与合规）、§12.5 / §12.6（企微接入异步与可靠性）、§13.3（v1 边界）、§13.5（数据看板最小指标集，含日报）、§13.6（技术架构）、§14.5（部署与运维）、§17.1.1（试运营要求）、§17.1.3（外部依赖降级）、§17.1.4（MVP 最低验收）、§17.2.4（删除命令细则）、§17.3（外部依赖确认单）
> 关联架构章节：`docs/architecture.md` §二（目录结构 `tasks/`）、§三（链路）、§五（消息数据流）、§七.4（Admin API 清单）
> 依赖阶段：Phase 1 ~ Phase 6 已全部完成并通过各自阶段验收
> 配套文档：
> - 开发实施文档：`collaboration/features/phase7-dev-implementation.md`
> - 开发 Checklist：`collaboration/features/phase7-dev-checklist.md`
> - 测试实施文档：`collaboration/features/phase7-test-implementation.md`
> - 测试 Checklist：`collaboration/features/phase7-test-checklist.md`

## 0. 本版修订说明

本文档在 2026-04-18 初稿的基础上，按两轮代码评审反馈修订了以下实现基线，以避免与项目既有基线冲突：

1. **定时任务部署**：由"独立 scheduler 容器 + 独立进程"改为"**app 进程内 APScheduler + Redis 分布式锁**"，与 `方案设计_v0.1.md:119 / :1098` 单进程部署基线和 `docs/implementation-plan.md §4.8` 的 5 容器 Docker 编排一致
2. **监控事件落地**：不再扩展 `audit_log.action` 枚举，运维事件统一走 **`loguru` 结构化日志**；`/删除我的信息` 等合规事件继续按 `delete_user_data()` 已用的 `target_type='user' + action='auto_pass'` 语义写入
3. **用户删除 7 天计时起点**：使用 `User.extra['deleted_at']`（现有 JSON 列，零 schema 变更），`services/user_service.delete_user_data()` 中补写入；**存储格式固定为 `YYYY-MM-DD HH:MM:SS` UTC 字符串**（不使用 `isoformat()`，避免 MySQL `STR_TO_DATE` 需要额外 trim）
4. **nginx SPA 与 Admin API 共路径冲突**：采用 `map $http_accept` + `$request_method` + `error_page 418 = @admin_api` + `recursive_error_pages on` 的 named-location 模式，`if` 块内只使用 `return`，**所有 `proxy_pass / proxy_set_header` 放在 named location 中**（nginx 官方不推荐 `if` + proxy directive 组合）；配合 Vite `base: '/admin/'` 修复静态资源路径
5. **Vite base 前置条件**：`frontend/vite.config.js` 必须配置 `base: '/admin/'` 并重新 build；否则 `index.html` 引用的 `/assets/*` 将 404 白屏
6. **群消息重试**：新增独立 `queue:group_send_retry`（payload 含 `chat_id`）；不复用现有 `queue:send_retry`（Phase 4 已定义为 `send_text(userid, content)`）
7. **P95 回复延迟口径**：以 `wecom_inbound_event.created_at`（实际字段名，非 `received_at`）到 **同一 `userid` 下首条** `conversation_log(direction='out', created_at >= in.created_at)` 的 `created_at` 为近似（Phase 4 `session_lock:{userid}` 保证用户消息串行）；可选精确口径通过给出站 `criteria_snapshot` 增补 `source_msg_id` 字段实现
8. **小程序点击事件 E2E**：按 `方案设计_v0.1.md:1391` 降级规则，改为"**有客户埋点时必测；无埋点时以推荐后二次追问率替代**"
9. **system_config key 命名**：统一为现有风格 `ttl.conversation_log.days` / `monitor.queue_incoming.threshold`；**监控阈值走 `.env` 或 seed 固定值**，不暴露到运营后台配置页（对齐 §13.3 "❌ 告警规则配置 v2 再做"）
10. **文档事实性**：`models.py` 实际 **12 张表**；`§13.5` 才是数据看板与日报；容器数量统一为 **5**
11. **task_lock 实现**：使用 owner token + Lua CAS 释放脚本，避免任务耗时超过 TTL 时误删其它实例的锁
12. **APScheduler 启动顺序**：先 `scheduler.start()` 再读 `job.next_run_time`（pending job 在 start 前没有该属性，访问会 `AttributeError`）
13. **横向扩容验证方式**：`docker-compose.prod.yml` 中 `app` 服务有 `container_name: jobbridge-app`，与 Compose `--scale` 冲突；分布式锁测试改用**两个本地 uvicorn 实例**验证，不作为 compose scale 验收项

## 1. 阶段目标

Phase 7 的目标，是把 Phase 1 ~ Phase 6 已构建的业务能力收口成一个可稳定运行、可运维、可上线的系统，并按《方案设计_v0.1.md》§17.1.4 的最低验收标准输出正式上线验收结果。

本阶段完成后，项目至少应具备以下能力：

- 具备**定时任务体系**：TTL 清理、7 天硬删除、Worker 心跳巡检、队列积压告警、出站补偿巡检、每日企微群日报均可在 app 进程内的 APScheduler 中稳定运行，并通过 Redis 分布式锁保证横向扩容时不重复触发
- `/删除我的信息` 从"立即软删除"到"7 天后硬删除"的完整闭环可被自动执行
- 具备**外部可观测能力**：LLM 调用日志、队列 / 消费 / 死信 / 出站补偿可观测；队列积压、Worker 离线等异常事件写 `loguru` 结构化日志并推送运营群（不新增 `audit_log.action` 枚举）
- **Docker 生产编排**打通：`nginx + app + worker + mysql + redis` **5 容器**在单机完整运行，前端构建产物由 nginx 托管
- **生产环境配置基线**落实：`CORS_ORIGINS`、公网回调域名、HTTPS 证书、防火墙端口、admin 初始密码等按 §17.3 完成
- **备份与恢复演练**完成：MySQL 定时备份 + Redis AOF + 上传目录备份 + 完整恢复演练有据可查
- **端到端联调**覆盖"企微入站 → Worker 处理 → 回复 → 运营后台审核 → 前端可视 → 事件回传 → 报表"完整链路
- 按 §17.1.4 的 7 条 MVP 指标完成 §17.1.1 要求的**至少 7 天试运营**并给出通过 / 不通过结论
- 输出一份正式**上线验收报告**与**上线 Checklist 执行记录**，由技术 + 运营双方签字归档

## 2. 当前代码现状

### 2.1 已具备的能力（Phase 1~6 交付）

- `backend/app/models.py`：**12 张表 ORM**（user / job / resume / conversation_log / audit_log / dict_city / dict_job_category / dict_sensitive_word / system_config / admin_user / event_log / wecom_inbound_event），`wecom_inbound_event` / `job.version` / `resume.version` / `conversation_log.wecom_msg_id` 已到位
- `backend/app/wecom/`：验签、解密、XML 解析、`WeComClient.send_text/download_media/get_external_contact` 已可用
- `backend/app/services/`：用户、意图、上传、审核、检索、会话、权限 7 个 service（Phase 3）+ `message_router`、`worker`、`command_service` 等（Phase 4）+ admin 系列 service（Phase 5）
- `backend/app/services/user_service.delete_user_data()`：已实现立即软删 + 写 `audit_log(target_type='user', action='auto_pass', reason='用户主动执行 /删除我的信息', operator='system')`；**尚未记录删除时间**，Phase 7 需补
- `backend/app/services/worker.py`：独立 Worker 进程 + 心跳（`worker:heartbeat:{pid}` TTL=120s）+ 启动自检 + 死信 + `queue:send_retry` 消费（payload `{userid, content, send_retry_count, backoff_until}`）
- `backend/app/api/`：`webhook.py`、`events.py`、`admin/*` 路由齐全
- `frontend/`：Vue 3 + Element Plus 运营后台 SPA 已完成，`frontend/dist/` 可构建
- `docker-compose.yml` / `docker-compose.prod.yml`：已包含 `app + worker + mysql + redis + nginx` 5 容器定义
- `backend/app/tasks/`：**目录存在但仅有 `__init__.py`**，无实际任务模块
- `nginx/nginx.conf`：反代基础配置已存在，但 `location /admin` 静态与 Admin API 共用 `/admin/*` 路径存在潜在冲突（见 §3.1 模块 G）
- `backend/sql/seed.sql`：已有 `ttl.job.days` / `ttl.resume.days` / `ttl.conversation_log.days` / `rate_limit.*`

### 2.2 当前缺失的能力

Phase 7 需要补齐：

- `backend/app/tasks/scheduler.py`：APScheduler 注册入口（由 `app/main.py` lifespan 启动，**非独立进程**）
- `backend/app/tasks/ttl_cleanup.py`：TTL 软删 + 7 天硬删
- `backend/app/tasks/daily_report.py`：每日 09:00 企微群日报
- `backend/app/tasks/worker_monitor.py`：Worker 心跳巡检 + 队列积压 + 死信告警
- `backend/app/tasks/send_retry_drain.py`：`queue:send_retry` / `queue:group_send_retry` 巡检与消费
- `backend/app/tasks/common.py`：Redis 分布式锁、loguru 结构化日志工具
- `backend/app/services/user_service.delete_user_data()`：补写 `User.extra['deleted_at']`
- `backend/app/wecom/client.py`：新增 `send_text_to_group(chat_id, content) -> bool`
- `nginx/nginx.conf`：修复 `/admin/*` SPA/API 共路径冲突
- LLM 调用日志统一打点
- MySQL / uploads 定时备份脚本
- 备份恢复演练记录
- `.env.example` 补 `CORS_ORIGINS` / `APP_ENV` / `SCHEDULER_TIMEZONE` / `DAILY_REPORT_CHAT_ID` / `MONITOR_*`
- 上线前 Checklist 执行记录
- 正式验收报告

### 2.3 Phase 4 遗留项

Phase 4 `phase4-main.md §3.1 模块 G` 明确把"外部定时巡检"延后到 Phase 7，Phase 7 独立完成：

- 每 3 分钟检查 `worker:heartbeat:*` 是否有有效 key，全部失联写 loguru `ERROR` + 推送运营群
- 每 1 分钟检查 `LLEN queue:incoming`，超过阈值写 loguru `WARN` + 推送运营群
- `LLEN queue:dead_letter > 0` 立即推送（首次超阈值推送一次，10 分钟窗口内去重）

## 3. 本阶段范围

### 3.1 必须完成的模块

#### 模块 A：APScheduler 内嵌 app 进程

- `app/tasks/scheduler.py` 提供 `register_scheduler(app: FastAPI) -> None`
- `app/main.py` 在 `lifespan` 启动阶段调用；使用 `BackgroundScheduler`（非 `BlockingScheduler`，因为它在 FastAPI 主进程内）
- 注册任务：
  - `ttl_cleanup`：cron `0 3 * * *`
  - `daily_report`：cron `0 9 * * *`
  - `worker_monitor.check_heartbeat`：interval 180s
  - `worker_monitor.check_queue_backlog`：interval 60s
  - `worker_monitor.check_dead_letter`：interval 60s
  - `send_retry_drain.check_backlog`：interval 600s
  - `send_retry_drain.drain_group_send_retry`：interval 30s（消费群消息重试队列）
- **横向扩容安全**：每个任务入口都用 Redis `SETNX task_lock:{task_name} 1 EX {ttl}` 分布式锁；若已被其它实例持有，本次跳过并打日志 "skipped, another instance running"
- **优雅退出**：FastAPI lifespan shutdown 时调用 `scheduler.shutdown(wait=False)`
- 不创建 `scheduler` Docker 容器；`docker-compose.yml` / `docker-compose.prod.yml` **保持 5 容器**不变

#### 模块 B：`tasks/ttl_cleanup.py` — TTL 清理与硬删除

执行频率：**每日 03:00**

覆盖范围（口径对齐 §7.7.4）：

| 数据 | 软删除条件 | 硬删除条件 |
|---|---|---|
| `job` | `expires_at < NOW()` 且 `delist_reason IS NULL` → `delist_reason='expired'` + `deleted_at=NOW()` | `deleted_at < NOW() - INTERVAL 7 DAY` 硬删 |
| `resume` | `expires_at < NOW()` 且 `deleted_at IS NULL` → `deleted_at=NOW()` | `deleted_at < NOW() - INTERVAL 7 DAY` 硬删（含敏感字段） |
| `conversation_log` | 无软删除 | `created_at < NOW() - INTERVAL 30 DAY` 直接硬删 |
| `audit_log` | 无软删除 | `created_at < NOW() - INTERVAL 180 DAY` 硬删 |
| `wecom_inbound_event` | 无软删除 | `created_at < NOW() - INTERVAL 30 DAY` 硬删 |
| 用户主动删除（`/删除我的信息`） | 命令触发时 `user.status='deleted'` 已立即设置，`User.extra['deleted_at']` 同步写入 | 扫描 `user.status='deleted' AND JSON_EXTRACT(user.extra,'$.deleted_at') < NOW() - INTERVAL 7 DAY`，硬删其所有 `resume` / `conversation_log` 残留；`user` 记录保留（防重复注册） |

约束：

- 每次任务运行把 **扫描数 / 软删数 / 硬删数 / 失败数** 写入 `loguru`（JSON 行，`logger.bind(event="ttl_cleanup_summary")`），不写 `audit_log`
- 失败必须记录异常堆栈、受影响主键到 `loguru.error`
- 硬删必须逐张表使用**分批删除**（`LIMIT 500 per batch`），每批独立 commit，避免锁表
- 删除 `resume` 时同步调用 `storage.delete(key)` 清理图片
- 未过期数据绝不能被误伤；每步 DELETE 前先 SELECT COUNT 打印到日志

#### 模块 C：`User.extra['deleted_at']` 写入

- `services/user_service.py:delete_user_data()` 末尾新增：
  ```python
  user = db.query(User).filter(User.external_userid == external_userid).one_or_none()
  if user is not None:
      extra = dict(user.extra or {})
      # MySQL 友好 UTC 字符串，便于 STR_TO_DATE 一次解析
      extra["deleted_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
      user.extra = extra
  ```
- **存储格式固定为 `%Y-%m-%d %H:%M:%S`**（UTC，无 `T`、无时区后缀）；**禁止使用 `isoformat()`**（其输出 `2026-04-18T12:34:56+00:00`，MySQL `STR_TO_DATE` 需额外 trim）
- 由于 `extra` 是 `MutableDict.as_mutable(sa.JSON)`，赋值为新 dict 会触发变更跟踪
- 7 天计时起点以 `User.extra['deleted_at']` 为准；若字段缺失（历史遗留用户），回落到"最新一条 `AuditLog(target_type='user', target_id=userid, action='auto_pass', operator='system', reason LIKE '%/删除我的信息%')` 的 `created_at`"
- SQL 比较一律使用 `STR_TO_DATE(..., '%Y-%m-%d %H:%i:%s') < UTC_TIMESTAMP() - INTERVAL 7 DAY`，详细查询见开发实施 §5.5
- MySQL 容器时区建议配置为 UTC（`default-time-zone='+00:00'`），否则 `audit_log.created_at` 兜底分支需要 `CONVERT_TZ`
- 该补丁属最小修复，不改 `delete_user_data()` 其它行为；Phase 7 允许修改

#### 模块 D：`tasks/daily_report.py` — 每日企微群日报

执行频率：**每日 09:00**

统计口径（对齐 §13.5、§17.1，数据走 `services/report_service.py` 已有能力，不写 SQL）：

- 今日 DAU（`last_active_at >= 今日 00:00`）
- 今日新增上传：岗位数 / 简历数
- 今日检索次数
- 今日推荐命中率 / 空召回率
- 今日审核打回率
- 今日新增封禁数
- 待审积压条数
- Worker 健康 / 死信数 / 队列最大长度
- 与昨日对比（↑ ↓ ±）

推送方式：

- 调用 `WeComClient.send_text_to_group(chat_id, content)`（本阶段补实现）
- 推送失败不视为硬性故障：写 `queue:group_send_retry`（模块 H），下一轮 drain 消费
- 企微群 ID 通过 `.env` 变量 `DAILY_REPORT_CHAT_ID` 配置；未配置时跳过推送，只 loguru `info("daily_report: chat_id empty, skip push")`
- 报表快照 **不写** `audit_log`（避免扩展枚举）；通过 loguru `logger.bind(event="daily_report")` 留痕

#### 模块 E：`tasks/worker_monitor.py` — Worker / 队列监控

三个独立检查函数：

| 巡检项 | 触发条件 | 触发动作 |
|---|---|---|
| `check_heartbeat` | 扫描 `worker:heartbeat:*` 无任何有效 key | `loguru.error(event="worker_all_offline")` + 推送运营群 |
| `check_queue_backlog` | `LLEN queue:incoming > $MONITOR_QUEUE_INCOMING_THRESHOLD`（默认 50） | `loguru.warn(event="queue_backlog")` + 推送运营群 |
| `check_dead_letter` | `LLEN queue:dead_letter > 0` | `loguru.error(event="dead_letter")` + 推送运营群 |

阈值来源：

- 阈值由 `.env` 提供（`MONITOR_QUEUE_INCOMING_THRESHOLD` / `MONITOR_SEND_RETRY_THRESHOLD` / `MONITOR_ALERT_DEDUPE_SECONDS`），**不进 `system_config` 管理页面**，对齐 §13.3 "告警规则配置 v2 再做"
- 首次部署用 seed 默认值；调整阈值需要发布或修改 `.env` 后 `docker compose up -d --no-deps app`

告警去重：

- 同一 event 在 10 分钟内只推送一次企微群（`alert_dedupe:{event_name}` SETNX EX 600）
- loguru 每次巡检都记录（便于运维按时间线排查）

#### 模块 F：`tasks/send_retry_drain.py` — 出站补偿巡检

职责：

- `check_backlog`：每 10 分钟检查 `queue:send_retry` 和 `queue:group_send_retry` 长度；超阈值触发告警（复用模块 E 告警通道）
- `drain_group_send_retry`：每 30 秒消费一条 `queue:group_send_retry`，payload 结构：
  ```json
  {
    "chat_id": "...",
    "content": "...",
    "retry_count": 0,
    "backoff_until": 1712000000.0
  }
  ```
  - 未到 `backoff_until` → 放回队尾
  - 调用 `wecom_client.send_text_to_group(chat_id, content)`
  - 成功 → 结束
  - 失败 → `retry_count + 1`，按 60s / 120s / 300s 退避；`retry_count >= 3` → `loguru.error(event="group_send_failed_final")` 后丢弃

**注意**：`queue:send_retry`（用户点对点消息）仍由 Phase 4 `worker.py` 消费，本模块**不重复消费它**；只做监控。

#### 模块 G：nginx SPA 与 Admin API 共路径冲突修复 + 静态资源基路径修复

问题 1：`nginx/nginx.conf` 现有 `location /admin { try_files ...; }` 会把 axios 发起的 `POST /admin/login` / `GET /admin/audit/queue` 也当作静态文件请求，fallback 到 `/admin/index.html`（HTML），前端解析报错。

问题 2：`frontend/dist/index.html` 在 Vite 默认 `base: '/'` 下引用的是 `/assets/xxx.js`；而 nginx 把 dist 挂到 `/usr/share/nginx/html/admin`，`location /admin` 无法服务 `/assets/*`，浏览器访问 `/admin/login` 即使拿到 HTML 也会因 JS/CSS 资源 404 白屏。

双重修复策略：

1. **前端**：`frontend/vite.config.js` 必须设置 `base: '/admin/'` 并重新 `npm run build`；此后 `index.html` 引用路径变为 `/admin/assets/...`，可被 `location /admin/` 的 `try_files` 命中
2. **nginx**：采用 named location + `error_page 418` 模式，`if` 块里只允许 `return`，所有 proxy directives 放在 `@admin_api` 中；避免 `if + proxy_set_header` 组合（nginx 官方不推荐，`-t` 可能通过但语义不可靠）

完整 `nginx/nginx.conf`（开发实施 §5.11 有同步说明）：

```nginx
map $http_accept $admin_is_api {
    default              0;
    "~*application/json" 1;
}

server {
    listen 80;
    server_name _;
    charset utf-8;
    client_max_body_size 10m;

    location = /nginx-health {
        return 200 "ok";
        add_header Content-Type text/plain;
        access_log off;
    }

    location /admin/ {
        error_page 418 = @admin_api;
        recursive_error_pages on;

        if ($request_method !~ ^(GET|HEAD)$) { return 418; }
        if ($admin_is_api = 1)               { return 418; }

        root /usr/share/nginx/html;
        try_files $uri $uri/ /admin/index.html;
    }

    location = /admin { return 302 /admin/; }

    location @admin_api {
        proxy_pass http://app:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }

    location / {
        proxy_pass http://app:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 30s;
    }
}
```

前端 axios 必须显式 `Accept: application/json`（Phase 6 实现已满足）。生产 HTTPS 证书挂载到 `listen 443 ssl;` 由运维按 §14.5 完善。

**验收必须**：

- 容器内 `nginx -t` 通过
- 浏览器 DevTools 加载 `/admin/login` 时 `/admin/assets/*.js` / `*.css` 全部 200
- `curl -X GET -H "Accept: application/json" http://localhost/admin/me` 返回 JSON

**如上线后出现兼容问题**（如部分 UA 的 Accept 组合被误判），降级方案是把 Admin API 改到 `/api/admin/*` 前缀并回写 `docs/architecture.md §7.4`；Phase 7 默认不启用。

#### 模块 H：`WeComClient` 扩展与群消息重试队列

- `wecom/client.py` 新增：
  ```python
  def send_text_to_group(self, chat_id: str, content: str) -> bool:
      # 调用 cgi-bin/appchat/send，errcode=0 → True；失败 → False（不抛异常）
  ```
- 新增 Redis 队列 `queue:group_send_retry`，**独立于** Phase 4 `queue:send_retry`（语义不同）
- 调用失败时由 `daily_report` / `worker_monitor` 入队；`send_retry_drain.drain_group_send_retry()` 消费
- 重试 3 次后 loguru `error(event="group_send_failed_final")`

#### 模块 I：LLM 调用日志补强

- `intent_service.classify_intent()` / `search_service.rerank()` 调用处统一打点
- 字段：`provider / model / prompt_version / input_tokens / output_tokens / duration_ms / intent / user_msg_id / status`（`ok / parse_failed / timeout / http_error`）
- 输出为 loguru 结构化 JSON 行（`logger.bind(event="llm_call")`）
- 不写 `audit_log`；`raw_response` 继续按 Phase 3 规则保留在 `conversation_log.criteria_snapshot`，不重复落库
- 不改 `llm/providers/*`，仅在 service 层打点

#### 模块 J：Docker 端到端编排

- **保持 5 容器**：`nginx + app + worker + mysql + redis`
- 定时任务随 app 进程启动，无需额外 service
- `docker-compose.prod.yml` / `docker-compose.yml` 保持现状；如 worker 或 app 使用 `Dockerfile` 基础镜像有变，补构建参数即可
- `.env.example` / `.env` 必须包含：
  - `APP_ENV=production`
  - `CORS_ORIGINS=https://<真实管理后台域名>`（**禁止 `*`**）
  - `SCHEDULER_TIMEZONE=Asia/Shanghai`
  - `DAILY_REPORT_CHAT_ID=`（可空）
  - `MONITOR_QUEUE_INCOMING_THRESHOLD=50`
  - `MONITOR_SEND_RETRY_THRESHOLD=20`
  - `MONITOR_ALERT_DEDUPE_SECONDS=600`
- 首次拉起后能 `curl https://<domain>/health` 返回 `{"status":"ok"}`

#### 模块 K：备份、恢复与上线配置

必须完成：

- `scripts/backup_mysql.sh`：每日 03:30 `mysqldump` 备份到宿主机，保留 14 天
- `scripts/backup_uploads.sh`：每日 04:00 打包 `uploads/` 到宿主机备份目录
- Redis AOF 已在 `docker-compose.prod.yml` 开启；本阶段补验证
- **恢复演练**：在预发环境执行一次完整 `mysqldump` 导入 + `uploads/` 还原 + Redis `BGREWRITEAOF`，并记录演练日志
- 上线前按 §17.3.3 清单逐项关闭：
  - 企微认证级别与出站 API 路径
  - 企微回调公网地址与 HTTPS
  - 隐私政策页链接
  - LLM 供应商账号与额度
  - 生产服务器与备份策略

#### 模块 L：端到端联调与验收指标实测

E2E 场景（参照 §10、§12、§13）：

| # | 场景 | 参与模块 |
|---|---|---|
| E1 | 新工人首次消息 → 自动注册 → 欢迎语 | webhook / worker / user_service |
| E2 | 工人求职 → 推荐 Top 3 → `更多` → 第二批 | 全链路 + conversation_snapshot |
| E3 | 厂家发布岗位 → 审核自动通过 → 工人检索到 | upload / audit / search |
| E4 | 厂家发布岗位 → 敏感词命中 → 进入审核队列 → 后台驳回 → 工人检索不到 | audit_workbench / search |
| E5 | 中介 `/找岗位` `/找工人` 切换 + 双向检索 | conversation / search |
| E6 | 工人 `/删除我的信息` → 立即软删 + `User.extra['deleted_at']` 落位 → 7 天后硬删 | command / ttl_cleanup |
| E7 | 厂家 `/下架` / `/招满了` / `/续期` | command |
| E8 | Worker kill 后 webhook 仍入队，Worker 重启后启动自检重放 | webhook / worker |
| E9 | 队列积压触发告警 + 运营群可见 | worker_monitor / wecom_client |
| E10 | 运营后台从 `pending` 审核到 `passed` / `rejected` + Undo | admin audit workbench |
| **E11** | 小程序点击事件回传 → 事件表落库 → 报表更新 | events / report_service |
| E12 | 每日 09:00 运营群收到日报 | daily_report |
| E13 | 每日 03:00 TTL 任务执行 + 7 天前删除用户资料被硬删 | ttl_cleanup |

**E11 降级规则**（对齐 `方案设计_v0.1.md:1391`）：

- **有客户小程序埋点**：E11 必测，"详情点击率"纳入验收
- **无客户埋点**：E11 跳过，验收改用"推荐后二次追问率"替代，并在 §17.3 外部依赖确认单标注"小程序埋点 = 有风险"

验收指标实测（必须完成 **§17.1.1 要求的至少 7 天试运营**，口径对齐 §17.1.4）：

- [ ] 结构化提取成功率 ≥ 85%
- [ ] 检索成功率 ≥ 95%
- [ ] 空召回率 ≤ 25%
- [ ] 人工审核打回率 ≤ 15%
- [ ] 死信率 ≤ 0.5%
- [ ] P95 端到端回复延迟 ≤ 5 秒（口径详见开发实施 §5.14.1：起点 `wecom_inbound_event.created_at`；终点同一 `userid` 下首条 `conversation_log(direction='out', created_at >= in.created_at)` 的 `created_at`；依赖 Phase 4 `session_lock:{userid}` 保证同用户消息串行；误差 < 1s。**不使用 `wecom_msg_id` 关联**，因为出站日志的 `wecom_msg_id` 固定为 NULL）
- [ ] `/删除我的信息` 完成率 = 100%

每项指标必须输出"定义 / 统计口径 / 数据来源 / 实测值 / 是否达标"五列汇总表。

#### 模块 M：上线 Checklist 执行与验收报告

- 按 §14.5.4 上线前 Checklist 完整走一遍，逐项截图或记录
- 按 §17.3 外部依赖确认单每一项标记"已确认 / 待确认 / 有风险"，`待确认` / `有风险` 必须附备选方案
- 输出两份文档并提交到 `collaboration/handoffs/phase7-release-report.md`（本阶段新增）：
  - **验收报告**：MVP 指标实测结果、E2E 场景通过情况、已知问题清单、遗留二期 Backlog
  - **上线 Checklist 执行记录**：逐项勾选与签字

### 3.2 本阶段明确不做

- 不新增业务功能
- 不做二期能力：RAG / 向量检索、headcount 自动递减、自动封禁规则、RBAC、Prompt 热更新、对话日志全文检索、实名核验、多租户、告警规则配置
- 不扩展 `audit_log.action` 枚举、不新增 `audit_log.extra` 列
- 不新增独立 scheduler 容器 / 独立进程
- 不把监控阈值暴露到运营后台配置页
- 不做 K8s / 微服务拆分
- 不在前端做新增页面；Phase 6 遗留 P0 缺陷如阻塞上线仅做修复
- 不跳过备份恢复演练就标注"上线就绪"
- 不在未完成 §17.3.3 5 项关闭清单的情况下发起上线

## 4. 真值来源与实现基线

出现冲突时，按以下优先级执行：

1. `docs/implementation-plan.md` §4.8
2. `方案设计_v0.1.md` §7.7、§12.5、§12.6、§13.3、§13.5、§14.5、§17.1.1、§17.1.3、§17.1.4、§17.2.4、§17.3
3. `docs/architecture.md` §二、§三、§五、§六、§七.4
4. `collaboration/features/phase4-main.md`（异步链路契约）
5. `collaboration/features/phase5-main.md`（事件回传、报表、审核契约）
6. `collaboration/features/phase6-main.md`（前端构建产物依赖）
7. 本文档

本阶段额外锁定以下实现约束：

- TTL 清理与硬删除必须是**分批**、**幂等**、**可重入**；单次失败不得污染其它表
- 所有定时任务只通过 **Redis 分布式锁**保证单实例运行；app 横向扩容场景下不得重复执行
- 所有任务必须写 **loguru 结构化日志**（`logger.bind(event=...)`），不得扩展 `audit_log.action` 枚举
- 企微群推送失败必须走 `queue:group_send_retry`（**独立队列**），不得复用 `queue:send_retry`
- 监控阈值走 `.env`，不走 `system_config` 页面
- `CORS_ORIGINS` 生产环境不得为 `*` 或为空；`config.py` 在 `APP_ENV=production` 时启动即校验
- admin 默认密码 `admin123` 必须在首次上线前替换
- 恢复演练必须在预发环境执行至少一次
- 备份脚本不得把密文或管理员哈希写进日志或备份文件名

### 4.1 Phase 7 新增配置

**`.env` 环境变量**（不进 admin 页面）：

| 变量 | 用途 | 默认值 |
|---|---|---|
| `APP_ENV` | 环境标识 | `development` |
| `CORS_ORIGINS` | 跨域白名单（多值用逗号） | 空（生产启动校验失败） |
| `SCHEDULER_TIMEZONE` | APScheduler 时区 | `Asia/Shanghai` |
| `DAILY_REPORT_CHAT_ID` | 日报推送企微群 ID | 空（跳过推送） |
| `MONITOR_QUEUE_INCOMING_THRESHOLD` | 队列积压阈值 | 50 |
| `MONITOR_SEND_RETRY_THRESHOLD` | 出站补偿队列阈值 | 20 |
| `MONITOR_ALERT_DEDUPE_SECONDS` | 告警去重窗口（秒） | 600 |

**`system_config` 表新增或确认 key**（沿用既有命名风格 `group.key.sub`）：

| key | 用途 | 默认值 | 现状 |
|---|---|---|---|
| `ttl.job.days` | 岗位 TTL（天） | 30 | 已有（seed.sql:53） |
| `ttl.resume.days` | 简历 TTL（天） | 30 | 已有（seed.sql:54） |
| `ttl.conversation_log.days` | 对话日志 TTL（天） | 30 | 已有（seed.sql:55） |
| `ttl.audit_log.days` | 审核日志 TTL（天） | 180 | **Phase 7 新增** |
| `ttl.wecom_inbound_event.days` | 入站事件表 TTL（天） | 30 | **Phase 7 新增** |
| `ttl.hard_delete.delay_days` | 软删到硬删延迟（天） | 7 | **Phase 7 新增** |

新增 key 必须同步更新 `backend/sql/seed.sql`。

## 5. 接口契约

### 5.1 任务函数约定（进程内）

每个任务模块暴露一个无参调用入口（由 APScheduler 调起）：

```python
def run() -> None: ...  # ttl_cleanup / daily_report
def check_heartbeat() -> None: ...
def check_queue_backlog() -> None: ...
def check_dead_letter() -> None: ...
def check_backlog() -> None: ...
def drain_group_send_retry() -> None: ...
```

内部统一使用 Redis 分布式锁与 loguru 打点（模板见 `tasks/common.py`）。任何抛出的异常由 APScheduler 默认 `EVENT_JOB_ERROR` 监听器捕获并写入 loguru，不影响后续任务。

### 5.2 `WeComClient` 扩展

```python
def send_text_to_group(self, chat_id: str, content: str) -> bool: ...
```

- 调用 `cgi-bin/appchat/send`
- `errcode=0` → True
- 其它错误返回 False 并 loguru `error`
- 不抛异常

### 5.3 `queue:group_send_retry` payload

```json
{
  "chat_id": "GROUP_ID",
  "content": "日报正文...",
  "retry_count": 0,
  "backoff_until": 1712000000.0
}
```

消费者：`tasks/send_retry_drain.drain_group_send_retry()`。

### 5.4 `/health` 端点扩展（可选）

可把 `/health` 从"查 DB"扩展为 `{"db": "ok", "redis": "ok", "queue_incoming": 3, "worker_heartbeat": true}`，但所有子检查合计耗时 < 200ms。

## 6. 验收标准

- [ ] `backend/app/tasks/` 下 scheduler / ttl_cleanup / daily_report / worker_monitor / send_retry_drain / common 文件齐备
- [ ] `app/main.py` lifespan 启动 APScheduler，日志输出任务注册表；shutdown 调 `shutdown(wait=False)`
- [ ] Docker 仍为 **5 容器**，`docker compose -f docker-compose.prod.yml up -d` 全部 healthy
- [ ] 本地启动两个 uvicorn app 实例共享同一 Redis / MySQL，定时任务只在一个实例触发（分布式锁有效）；**不使用 `docker compose --scale app=2`**（与 `container_name: jobbridge-app` 冲突）
- [ ] TTL 任务能正确区分 `job.expires_at` / `resume.expires_at` / `conversation_log.created_at` / `audit_log.created_at` / `wecom_inbound_event.created_at`，分批执行并写 loguru 汇总
- [ ] `/删除我的信息` 立即写入 `User.extra['deleted_at']`；7 天后该用户 `resume` / `conversation_log` 全部被硬删；`user` 记录保留且 `status='deleted'`
- [ ] 每日 09:00 运营群收到日报，字段完整、昨日对比正确
- [ ] Worker 心跳缺失、队列积压、死信 > 0 都能正确告警且 10 分钟内不重复
- [ ] 群日报推送失败写入 `queue:group_send_retry`，并被 `drain_group_send_retry` 消费；不污染 `queue:send_retry`
- [ ] LLM 调用日志 loguru 可按 msg_id / userid / prompt 版本 / 时间范围检索
- [ ] nginx `/admin/*` 对 axios JSON 请求返回 API 响应，对浏览器 HTML 请求返回 SPA
- [ ] 前端通过 nginx 可登录 / 审核 / 导出 / 查看对话日志，无白屏
- [ ] MySQL 备份脚本执行后生成 `.sql.gz`；恢复演练可在预发环境完整还原
- [ ] `CORS_ORIGINS` 生产必填校验生效
- [ ] §17.1.4 七项指标按 §17.1.1 至少 7 天试运营后全部达标（E11 按降级规则执行）
- [ ] §14.5.4 上线前 Checklist 所有项勾选
- [ ] §17.3.3 5 项必关闭项全部 `已确认`
- [ ] `collaboration/handoffs/phase7-release-report.md` 验收报告 + 上线 Checklist 执行记录已归档

## 7. 进入条件

| 条件 | 状态 | 说明 | 如未满足的应对 |
|---|---|---|---|
| Phase 4 主链路验收通过 | 待确认 | webhook / worker / message_router 已稳定 | Phase 7 不具备入口条件 |
| Phase 5 admin API 验收通过 | 待确认 | 审核、账号、岗位、报表等接口可用 | 后台联调场景无法执行 |
| Phase 6 前端构建产物可用 | 待确认 | `frontend/dist/` 可 nginx 托管 | 先回归 Phase 6 |
| 生产服务器就绪 | 待确认 | 规格、系统、磁盘、带宽满足 §17.3 | 使用预发或临时云主机做演练 |
| 企微认证级别 + 回调域名 | 待确认 | §17.3 高优先级项 | E2E 联调降级为 Mock 出站，但正式验收不可通过 |
| LLM API 账号额度 | 待确认 | QPS 与日预算 30~80 元 | 暂停试运营 |
| 备份目录空间 | 待确认 | 至少保留 14 天备份所需容量 | 先扩容 |
| 小程序点击埋点 | 待确认 | E11 / "详情点击率"依赖项 | 按 §3.1 模块 L 降级规则执行 |

## 8. 风险与备注

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| 企微群消息权限不足 | 日报 / 告警无法推送 | 推送失败写 `queue:group_send_retry`，不阻塞；客户上线前申请群消息权限 |
| TTL 硬删误伤未过期数据 | 数据丢失 | 每步 DELETE 前 SELECT COUNT 打日志；分批；演练必须在预发跑一次 |
| MVP 指标不达标 | 上线决策受阻 | §17.1.1 要求至少 7 天试运营；未达标项进入二期 Backlog 或推迟上线 |
| APScheduler 与 uvicorn `--workers > 1` 叠加触发 | 任务重复执行 | Redis 分布式锁兜底；文档标注生产 `--workers` 推荐 1~2 + 横向扩 |
| 恢复演练未完成就上线 | 生产数据不可恢复 | 强阻塞项 |
| nginx `/admin/*` 判定规则覆盖不全 | 特殊请求获得错误响应 | 降级路径：API 迁到 `/api/admin/*` 并回写架构文档 |
| `User.extra['deleted_at']` 历史遗留缺失 | 7 天硬删计时无起点 | 回落到最新 AuditLog 起点；可一次性 backfill 脚本 |
| 生产 `CORS_ORIGINS` 配置成 `*` | 跨域风险 | `config.py` 在 `APP_ENV=production` 时拒绝 `*` 或空 |
| 插入二期需求 | 阶段拖延 | 所有新需求走 §8 变更控制 |

## 9. 文件变更清单

| 操作 | 文件 / 目录 | 说明 |
|---|---|---|
| 新建 | `backend/app/tasks/scheduler.py` | APScheduler 注册入口（由 app lifespan 调起） |
| 新建 | `backend/app/tasks/ttl_cleanup.py` | TTL 软删 + 硬删 |
| 新建 | `backend/app/tasks/daily_report.py` | 日报生成 + 推送 |
| 新建 | `backend/app/tasks/worker_monitor.py` | Worker 心跳 + 队列巡检 |
| 新建 | `backend/app/tasks/send_retry_drain.py` | 群消息重试消费 + 出站补偿巡检 |
| 新建 | `backend/app/tasks/common.py` | Redis 分布式锁 + loguru 工具 |
| 修改 | `backend/app/main.py` | lifespan 启动 / 关闭 APScheduler |
| 修改 | `backend/app/services/user_service.py` | `delete_user_data()` 补写 `User.extra['deleted_at']` |
| 修改 | `backend/app/wecom/client.py` | 新增 `send_text_to_group()` |
| 修改 | `backend/app/core/redis_client.py` | 如需补 `redis_lock` helper |
| 修改 | `backend/sql/seed.sql` | 补 `ttl.audit_log.days` / `ttl.wecom_inbound_event.days` / `ttl.hard_delete.delay_days` |
| 修改 | `nginx/nginx.conf` | SPA/API 共路径冲突修复 |
| 修改 | `.env.example` | 补 `APP_ENV` / `CORS_ORIGINS` / `SCHEDULER_TIMEZONE` / `DAILY_REPORT_CHAT_ID` / `MONITOR_*` |
| 修改 | `backend/app/config.py` | 生产环境 `CORS_ORIGINS` 校验 + 新变量读取 |
| 新建 | `scripts/backup_mysql.sh` | MySQL 定时备份 |
| 新建 | `scripts/backup_uploads.sh` | 上传目录定时备份 |
| 新建 | `scripts/restore_drill.sh` | 恢复演练参考脚本（注释严禁在生产执行） |
| 新建 | `collaboration/handoffs/phase7-release-report.md` | 验收报告 + 上线 Checklist 执行记录 |
| 不改动 | `docker-compose.yml` / `docker-compose.prod.yml` | **保持 5 容器**；如 Dockerfile 基础镜像升级再议 |
| 不改动 | `backend/app/models.py` | 不扩展 `audit_log.action` 枚举；不新增 `audit_log.extra` 列 |
| 可能修改 | `backend/app/services/report_service.py` | 日报查询函数如未暴露则补接口（Phase 5 已基本具备） |
| 可能修改 | `backend/app/services/intent_service.py` / `search_service.py` | LLM 调用点补 loguru 结构化打点 |
