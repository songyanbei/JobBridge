# Phase 7 上线验收报告（模板）

> ⚠️ **本文件目前是空白模板，不构成验收证据。**
> Phase 7 的"正式验收"出口要求：U3（企微 / LLM 真实依赖）关闭后启动 §17.1.1
> 至少 7 天试运营，按 §17.1.4 七项 MVP 指标记录实测，再由技术 + 测试 + 运营
> 三方在 §8 签字。在此之前，本文件**不能**被视为已签收的验收报告。
>
> 当所有 ⬛ 占位符（`YYYY-MM-DD` / `<git sha>` / `__%` / 未勾选 checkbox）都被
> 真实数据替换、§3 / §4 全部勾选 / 标记 `已确认`、§8 三方签字齐全后：
>   1. 把顶部 `状态: template` 改为 `approved`
>   2. 删除本警告框
>   3. 移除标题中的"（模板）"二字
>
> 不允许伪造或预填指标 / 签字 —— Codex 评审与上线 checklist 都会回查。

> 基于：`collaboration/features/phase7-main.md` §3.1 模块 M
> 配套：`collaboration/features/phase7-test-checklist.md`、`方案设计_v0.1.md` §17.1.4 / §14.5.4 / §17.3
> 状态：`template`（依次：template → draft → under-review → approved）
> 创建日期：2026-04-19
> 责任人：技术负责人 + 运营负责人

---

## 0. 元信息

| 字段 | 值 |
|---|---|
| 报告版本 | v0.1 |
| 上线目标日期 | YYYY-MM-DD |
| 试运营窗口 | YYYY-MM-DD ~ YYYY-MM-DD（至少 7 天，对齐 §17.1.1） |
| 技术签字 | 姓名 + 日期 |
| 运营签字 | 姓名 + 日期 |
| 关联 PR / commit | `<git sha>` |

---

## 1. §0.1 U1~U5 闭环状态

逐项填表；**`待确认` / `有风险` 必须附备选方案**。

| # | 项 | 当前状态 | 证据（日志 / 截图 / commit） | 备注 |
|---|---|---|---|---|
| U1 | Docker prod 5 容器编排 | ✅ / ⚠️ / ❌ | `docker compose -f docker-compose.prod.yml ps` 截图 + 压测报告 | 上线前需重跑一次作为 M5 签收证据 |
| U2 | 旧库 system_config 迁移 | ✅ | 迁移 SQL 执行日志 + `select * from system_config where config_key like 'ttl.%'` | `phase7_001_ensure_system_config.sql` 已上线 |
| U3 | 外部企微 / LLM 依赖 | ✅ / ⚠️ / ❌ | 企微回调日志 + LLM 调用样本日志 | 按 §17.3.3 5 项关闭清单 |
| U4 | MVP 7 天试运营指标 | ✅ / ⚠️ / ❌ | §17.1.4 七项指标实测表 | U3 关闭后启动 |
| U5 | passlib/bcrypt 兼容性告警 | ✅ | `requirements.txt` pin + 重启后 `loguru` 无 `__about__` warn | — |

---

## 2. §17.1.4 七项 MVP 指标实测

| # | 指标 | 定义 | 统计口径 | 数据来源 | 实测值 | 是否达标 |
|---|---|---|---|---|---|---|
| 1 | 入站消息处理成功率 | 完成消费的入站消息 / 入站总条数 | `wecom_inbound_event` 进入到 worker ack 计数 | loguru `worker_msg_done` / 入站表 | __% | ✅ / ❌ |
| 2 | P95 端到端回复延迟 ≤ 5s | 入站 → 首条出站 | 详见 phase7-main.md §3.1 模块 L 注 | loguru + conversation_log | __ms | ✅ / ❌ |
| 3 | 检索命中率 | 命中条数 / 检索请求 | `report_service.get_dashboard()` | dashboard | __% | ✅ / ❌ |
| 4 | 空召回率 | 空结果 / 检索请求 | 同上 | dashboard | __% | ✅ / ❌ |
| 5 | 人工审核打回率 ≤ 15% | 打回 / 总审核动作 | `audit_log` action 分组 | daily_report 内 `_audit_reject_rate` | __% | ✅ / ❌ |
| 6 | 死信率 ≤ 0.5% | 死信 / 入站总条数 | `LLEN queue:dead_letter` 累计 vs 入站 | loguru + redis | __% | ✅ / ❌ |
| 7 | `/删除我的信息` 完成率 = 100% | 收到指令 → 7 天硬删 | 抽样审核 | audit_log + ttl_cleanup loguru | __% | ✅ / ❌ |

> E11（小程序点击事件回传）若按 §17.1.3 降级规则执行，需在备注列写明降级口径。

---

## 3. §14.5.4 上线前 Checklist 执行记录

逐项勾选；任一未勾不得签字。

### 3.1 配置基线
- [ ] `.env` 已替换默认值（DB / Redis / JWT / `WECOM_*` / `LLM_*`）
- [ ] `APP_ENV=production`
- [ ] `CORS_ORIGINS` 已填具体域名，**不含 `*`**
- [ ] `TZ=Asia/Shanghai`，容器内 `date` 与 `python -c "import datetime; print(datetime.datetime.now())"` 时间一致
- [ ] `SCHEDULER_TIMEZONE=Asia/Shanghai`，启动日志 `next_run` 显示 `+08:00`
- [ ] `ADMIN_FORCE_PASSWORD_CHANGE=true`
- [ ] `ADMIN_DEFAULT_PASSWORDS` 至少包含 `admin123`（可追加企业自有的弱口令）
- [ ] admin 默认密码 `admin123` 已替换；**以默认口令登录后**任何业务接口返回 40301
- [ ] **以默认口令登录**：返回体 `password_changed=false`，loguru 出现
      `default password detected, force password_changed=0`
- [ ] **改密接口**：把新密码改成 `admin123` 应返回 40101 "新密码不能使用系统默认/弱口令"
- [ ] `DAILY_REPORT_CHAT_ID` 已填或显式确认空（空时只 loguru 不推送）
- [ ] `MONITOR_QUEUE_INCOMING_THRESHOLD` / `MONITOR_SEND_RETRY_THRESHOLD` / `MONITOR_ALERT_DEDUPE_SECONDS` 已确认

### 3.2 数据库
- [ ] `schema.sql` / `seed.sql` / `seed_cities_full.sql` 已导入
- [ ] `phase7_001_ensure_system_config.sql` 已执行；`select count(*) from system_config where config_key like 'ttl.%'` ≥ 6
- [ ] `system_config` 中 6 个 `ttl.*` key 值已按业务约定校对
- [ ] MySQL 备份脚本 `scripts/backup_mysql.sh` 至少执行过一次，生成 `.sql.gz`
- [ ] 备份恢复演练在预发完整跑通一次（数据库 + Redis AOF + uploads）

### 3.3 容器与网络
- [ ] `docker compose -f docker-compose.prod.yml up -d --build` 5 容器全部 healthy
- [ ] `nginx -t` 通过；`/health`、`/admin/login`、`/admin/me` 走 nginx 全部正常
- [ ] HTTPS 证书已部署，浏览器无 mixed content
- [ ] 仅必要端口对公网暴露（80/443），mysql/redis/8000 不暴露
- [ ] 企微回调域名已配置 + `/wechat/callback` 验签 200
- [ ] worker 容器日志无 `corpsecret missing` / `LLM_API_KEY missing`

### 3.4 监控与告警
- [ ] APScheduler 启动后 7 个任务全部 `scheduler running`，`next_run` 时区 `+08:00`
- [ ] worker 心跳 key `worker:heartbeat:*` 持续存在
- [ ] 模拟 Worker 全部 kill 后 3 分钟内 loguru 出现 `worker_all_offline` + 群消息推送（如 chat_id 已配）
- [ ] 死信队列写入 1 条后 1 分钟内出现 `dead_letter` 告警
- [ ] daily_report 9:00 推送成功（或群权限缺失时进 `queue:group_send_retry`）
- [ ] LLM 调用 loguru 可按 `userid` / `prompt 版本` / 时间范围检索

### 3.5 依赖
- [ ] §17.3.3 5 项关闭清单全部 `已确认`
- [ ] 企微群消息权限已申请通过
- [ ] LLM API 额度足够覆盖 7 天试运营

---

## 4. §17.3 外部依赖确认单

| # | 项 | 状态（已确认 / 待确认 / 有风险） | 备选方案 |
|---|---|---|---|
| 1 | 企微 corp_id / agent_id / secret | | Demo 环境兜底见 `docs/phase4-demo-env.md` |
| 2 | 企微回调域名 + HTTPS 证书 | | — |
| 3 | 企微群消息权限 | | 失败入 `queue:group_send_retry` |
| 4 | LLM 真实供应商账号 + 额度 | | 二供应商热备 |
| 5 | OSS 桶 / ACL（若启用） | | local 模式过渡 |

---

## 5. E2E 场景通过情况

按 phase7-test-checklist §G/H/I 走一遍，记录通过 / 失败：

| 场景 | 通过 | 备注 |
|---|---|---|
| 企微入站 → Worker 处理 → 回复 | | |
| 运营后台审核 → 前端可视 | | |
| 事件回传 API（X-Event-Api-Key） | | |
| `/删除我的信息` → 立即软删 → 7 天硬删 | | |
| 日报 09:00 推送 | | |
| 监控告警去重 10 分钟窗口 | | |

---

## 6. 已知问题清单 / 二期 Backlog

| # | 类型（缺陷 / 优化 / Backlog） | 描述 | 优先级 | 处置 |
|---|---|---|---|---|
| 1 | | | | |

---

## 7. 回归与单测覆盖

| 项 | 数据 |
|---|---|
| 单测总数 | __ |
| 通过 | __ |
| 失败 | __ |
| 集成测试 (`RUN_INTEGRATION=1`) | __ 通过 / __ 失败 |
| Phase 7 任务模块覆盖率 | ttl_cleanup / daily_report / worker_monitor / send_retry_drain / common 单测均存在 |
| Codex / 二评（如有） | 报告链接 |

---

## 8. 签字

| 角色 | 姓名 | 日期 | 签字 |
|---|---|---|---|
| 技术负责人 | | | |
| 测试负责人 | | | |
| 运营负责人 | | | |

> 全部"是否达标"列与 §3 Checklist 全部勾选完成、§4 全部 `已确认`、§6 无 P0 缺陷，方可签字上线。
