# 简历 Phase 11 完整测试方案与验证报告 v1.0

## 1. 测试结论摘要

本报告按“最小可执行单元”组织测试：每个用例只验证一个可观察边界，Phase 11
发布门禁串联这些小单元，不使用难以定位问题的巨型 E2E。Phase 11 后端/MySQL/Redis
专项门禁通过：`202 passed, 0 failed, 0 skipped`；管理端前端 Vitest `16 passed`、
lint 和生产构建通过；mock-testbed 后端 `41 passed`、mock-testbed 前端生产构建通过。
需求矩阵中的 Redis outage 故障用例也已在停止并恢复隔离 worker 后通过。

真实预发布/生产迁移、灰度、硬删除、脱敏快照、10 万条容量和生产 EXPLAIN 不在
本次授权范围内，报告不伪造这些证据。

## 2. 版本与环境

| 项目 | 值 |
|---|---|
| 工作树 | `D:\work\JobBridge-resume-expiry-full-update-v01` |
| 分支 | `codex/resume-expiry-full-update-v01` |
| 提交 | `d89ded6ec1087b69050c629d93e5056e3ca1cfb4` |
| API/worker 构建 | build `251`，SHA `d89ded6ec1087b69050c629d93e5056e3ca1cfb4` |
| MySQL | 隔离容器 `jb-p11-browser-mysql`，宿主端口 `33331` |
| Redis | 隔离容器 `jb-p11-browser-redis`，宿主端口 `36331`，DB `15` |
| 浏览器服务 | API `18090`、模拟 API `18091`、模拟前端 `35174`、管理端 `35173` |
| 测试时间 | 2026-08-23（Asia/Shanghai） |

隔离 MySQL 仅为动态建库夹具临时授予测试账号全局 DDL/PROCESS 权限；不触碰其他
`jobbridge-*` 服务或数据库。

服务复核：从 WSL 容器内访问 API `/health` 和 `/ready` 均为 HTTP 200，build
number/SHA 与上表一致；Windows 侧 `Invoke-WebRequest` 对 WSL 端口偶发 502，属于
WSL NAT 转发观测差异，浏览器验收使用应用内浏览器和 WSL 内探针结果。

## 3. 最小用例设计与需求追踪

| 用例 | 需求边界 | 前置 | 步骤 | 期望 | 实际 |
|---|---|---|---|---|---|
| P11-S1-01 | additive DDL 与 nullable DTO | 空库/旧库 | runner `check/apply` | 只新增结构，旧读写可启动 | 通过 |
| P11-S1-02 | manifest checksum | manifest | 修改副本 checksum 后 `check` | fail closed | 通过 |
| P11-S1-03 | ledger 断点/崩溃续跑 | 部分 ledger | `resume` | 从最后水位继续且幂等 | 通过 |
| P11-S1-04 | up/down 回退 | 已验证迁移 | `down`/`verify` | 恢复非空 legacy TTL，保留软删候选 | 通过 |
| P11-S1-05 | 媒体/孤儿/legacy 对账 | 隔离数据 | 执行三个 reconcile 脚本 | 可断点，异常阻断 verify | 通过 |
| P11-S2-01 | 激活时 UTC 双写 | 新上传/审核事务 | 激活并读取生命周期列 | `activated_at` 起算 TTL，UTC-naive | 通过 |
| P11-S2-02 | 双写回滚 | 注入事务失败 | 回滚激活 | 无半写状态 | 通过 |
| P11-S2-03 | 搜索/分页可见性 | 在线、候选、历史、过期简历 | 搜索及翻页 | 只返回在线候选，排除过期/下架/历史 | 通过 |
| P11-S2-04 | `/我的状态` 投影 | 同一用户多版本 | 查询状态 | 区分在线、候选、历史 | 通过 |
| P11-S3-01 | 更新入口与精确别名 | worker，rollout enabled | 发送 `/更新简历 [ID]` 及别名 | 仅 worker 可用，拒绝部分修改 | 通过 |
| P11-S3-02 | 空白多轮草稿 | 有旧简历 | 开始更新并补城市 | 草稿不继承旧字段，缺字段只追问一次 | 通过 |
| P11-S3-03 | 新 ID 与媒体隔离 | 旧简历在线 | 完成全量草稿 | 新候选 ID，媒体只绑定候选 | 通过 |
| P11-S3-04 | 幂等与并发唯一 | 相同 operation/message | 重放及并发创建 | 单一活动关系，cohort 不改组 | 通过 |
| P11-S4-01 | 审核/替换状态机 | awaiting_review 候选 | 审核通过/驳回 | 原子切换，旧简历历史 | 通过 |
| P11-S4-02 | 过期先/审核先锁顺序 | MySQL 两事务 | 交叉提交 | 无双激活、无死锁；只允许 base+1/expired 例外 | 通过 |
| P11-S4-03 | 冲突/取消/重试 | 冲突候选 | 管理/用户操作 | 稳定错误原因和可重试状态 | 通过 |
| P11-S4-04 | 管理员编辑/延期/下架/删除 | operator/super_admin | 分别操作 | RBAC 和状态限制生效 | 通过 |
| P11-S5-01 | 推荐持久化前复核 | 到期简历与推荐事实 | 晚到写 | `recommendation_target_stale`，不产生 request/attempt | 通过 |
| P11-S5-02 | outbox claim 二次复核 | 已 claim outbox | 提交推荐事实 | 二次锁定后拒绝过期目标 | 通过 |
| P11-S5-03 | Redis revocation fence | cleanup worker | Redis fence 后清理 | fence、DB 清理、会话失效同一成功闭环 | 通过 |
| P11-S5-04 | 过期/候选回收 | due rows | 批处理、续租、continuation | 终态 reason 精确，失败可重试 | 通过 |
| P11-S5-05 | 硬删除门禁 | dead-letter/媒体/孤儿非零 | 尝试硬删 | 门禁阻断；全部清零后才允许 | 通过 |
| P11-S6-01 | 后台筛选/投影/隐私 | admin UI/API | 查看在线/历史/冲突 | 不泄露 userid、正文、URL/object key | 通过 |
| P11-S6-02 | dead-letter 重驱 | operator/super_admin | 查询、理由、批量重驱 | 每批最多 50，每分钟最多 2 批，脱敏审计 | 通过 |
| P11-S6-03 | 媒体双人审批 | 两个不同管理员 | 审批后执行 | 自批拒绝，逐项结果和审计完整 | 通过 |
| P11-FE-01 | 生命周期徽标/空时间 | 管理端组件 | 渲染在线/候选/历史/空时间 | 文案和时间稳定，不显示 NaN | 通过 |
| P11-FE-02 | 操作按钮权限/冲突禁用 | 管理端组件 | 切换角色/冲突状态 | 按权限和状态禁用/隐藏 | 通过 |
| REG-01 | 原有岗位生命周期 | 隔离 MySQL/Redis | 岗位上传、审核、搜索、TTL、媒体清理 | 原有行为不回归 | 已纳入全量回归 |
| REG-02 | 模拟企微契约 | mock SQLite/fakeredis | users、OAuth、code2userinfo、inbound、SSE | 字段、幂等、CJK、SSE header 正确 | 通过 |

自动化需求明细与测试文件的逐项映射见
[简历Phase11需求测试追踪矩阵.md](D:\work\JobBridge-resume-expiry-full-update-v01\docs\简历Phase11需求测试追踪矩阵.md)。

## 4. 自动化执行记录

### 4.1 Phase 11 发布门禁

命令（环境变量均指向本轮隔离端口）：

```text
RUN_INTEGRATION=1
PHASE11_TEST_MYSQL_DSN=mysql+pymysql://jobbridge:***@127.0.0.1:33331/jobbridge
PHASE11_TEST_REDIS_DSN=redis://127.0.0.1:36331/15
DB_HOST=127.0.0.1 DB_PORT=33331 DB_NAME=jobbridge
DB_USER=jobbridge DB_PASSWORD=*** REDIS_HOST=127.0.0.1
REDIS_PORT=36331 REDIS_DB=15
python backend/scripts/phase11_release_gate.py release
```

结果：`202 passed, 0 failed, 0 skipped, 49 warnings`，退出码 `0`。JUnit
文件由 gate 在临时目录生成；终端明确输出 `Phase 11 release units passed:
tests=202, skipped=0`。

### 4.2 管理端前端

```text
npm run lint
npm test -- --run
npm run build
```

结果：lint 退出码 `0`；Vitest 4 个文件、`16 passed`；Vite 生产构建退出码 `0`
（2296 modules transformed）。

### 4.3 mock-testbed

后端命令：

```text
python -m pytest -q --junitxml=/tmp/jobbridge-mock-backend.xml mock-testbed/backend/tests
```

结果：`41 passed, 0 failed, 0 skipped, 1 warning`，退出码 `0`。
为使隔离测试真正可执行，修复了 SQLite 跨线程共享、函数级数据库隔离、SQLite
自增主键映射和无限 SSE header 测试夹具；这些修改不改变模拟服务生产协议。

前端命令：`npm install --no-package-lock; npm run build`。结果：Vite 构建退出码
`0`，`1671 modules transformed`。仓库无 lockfile，因此没有宣称 `npm ci` 通过；
安装过程报告 2 个依赖审计告警，未执行自动升级。

### 4.4 原有链路最小回归

按需求矩阵执行岗位过期/候选回收/媒体清理/Redis checkpoint 的最小文件集。
首次结果为 `40 passed, 1 skipped`（`/tmp/jobbridge-matrix-regression.xml`），唯一
skip 是 Redis outage 容器未配置。随后创建独立 `jb-p11-outage-redis`，暂停本轮
`jb-p11-browser-worker` 以避免 `FOR UPDATE SKIP LOCKED` 抢占测试行，单独执行 outage
用例得到 `1 passed`（`/tmp/jobbridge-redis-outage.xml`），恢复 worker 后服务健康。
因此矩阵证据最终为 `41 passed, 0 failed, 0 skipped`。

### 4.5 后端全量回归

已在同一隔离 MySQL/Redis 启动：

```text
python -m pytest -p no:cacheprovider -q \
  --junitxml=/tmp/jobbridge-backend-full.xml backend/tests
```

该命令已启动但因历史集成套件长时间运行（约 8 分钟仅完成约 4%）而停止，未生成
可用的完整统计；因此不能把“后端全量回归”记为通过。Phase11 专项门禁和第 4.4
节矩阵文件集仍是已执行证据，真实发布前应在 CI 资源充足时重新跑完整套件。

## 5. 浏览器核心链路验收

使用隔离模拟对话页和管理后台完成以下人工验收，API/worker build 均为
`251/d89ded6ec1087b69050c629d93e5056e3ca1cfb4`：

1. `/帮助` 返回帮助内容；求职搜索“我想找深圳的打包工”返回岗位；厂家发布
   “招几个深圳焊工，月薪8000”成功创建岗位。
2. 完整简历首次上传成功；`/我的状态` 显示在线生命周期。
3. `/更新简历` 进入空白多轮草稿；不继承旧简历字段；缺城市时追问，补城市后不重复追问。
4. 完成更新后生成新简历 `#2`，旧简历 `#1` 进入历史；验证原子替换和新 ID。
5. 管理后台 rollout 页面只展示 revision/人数，不泄露 userid；在线/历史简历、
   清理任务、媒体死信、媒体隔离页面均可打开。
6. 模拟对话与管理后台控制台无 error；仅存在既有 Element Plus `el-radio` label
   弃用警告，不影响功能。

## 6. 问题与处置

| 问题 | 根因 | 处置 | 结果 |
|---|---|---|---|
| Stage1 动态建库拒绝 | 测试账号无 CREATE/DROP/PROCESS | 仅隔离容器授予临时权限 | 之后门禁全绿 |
| Stage5 fence 单测失败 | 命令未设置 `DB_*`，误连默认旧库 | 对齐 DB/Redis 隔离端口 | 单测通过 |
| mock SQLite 无表/跨线程 | 默认线程池与内存库不兼容 | `StaticPool` + `check_same_thread=False` | 通过 |
| mock 测试数据串用 | `_engine` session 级 | 改函数级 fixture | 通过 |
| mock inbound 被误判重复 | SQLite BIGINT 不自增 | SQLite 映射 Integer | 通过 |
| SSE header 测试挂起 | TestClient 等待无限流结束 | 直接检查 StreamingResponse header | 通过 |
| Redis outage session reconciler | 运行中的隔离 worker 先 claim 测试行 | 测试期间暂停同一隔离 worker，完成后恢复 | 通过 |

## 7. 未执行与发布方证据

以下项目明确未在本次隔离环境执行，不能作为“通过”发布：

- 真实预发布/生产迁移、灰度、停写和 down migration；
- 真实对象存储、真实企业微信、真实 Redis/MySQL 故障演练；
- 10 万条积压、生产数据量级 continuation/租约耗时；
- `EXPLAIN ANALYZE` 对 `idx_resume_hard_delete` 的生产计划证据；
- 生产脱敏快照预检、备份恢复和逐实例 build/capability 清单核验。

执行这些项目须按 [简历Phase11发布回退与清理Runbook.md](D:\work\JobBridge-resume-expiry-full-update-v01\docs\简历Phase11发布回退与清理Runbook.md)
审批，并保留脱敏证据。五个开关在本次验证中保持关闭。

## 8. 验收结论

在当前隔离环境和当前提交上，Phase 11 核心链路、原有关键清理链路、前端组件、
模拟企微契约和浏览器业务流程均已通过最小可执行测试。历史后端全量总套件因运行
时间过长未完成，不能据此宣称全量回归通过；真实环境项目仍保持发布阻断，不能由
本报告替代审批。
