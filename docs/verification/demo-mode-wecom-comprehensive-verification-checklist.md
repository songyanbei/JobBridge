# JobBridge 演示模式、企微长连接与身份绑定综合验收清单

> 验收范围：独立演示工作区、企业微信智能机器人 WebSocket 长连接、AIBot 身份解析/绑定、同一企微账号三角色体验，以及演示数据下架和清理。
> 前置基线：[12 企业微信智能机器人 WebSocket 长连接改造说明](../architecture/12-wecom-aibot-websocket-migration.md)、[13 身份获取与角色绑定实施方案](../architecture/13-wecom-aibot-identity-role-binding-implementation-plan.md)、[14 演示模式独立隔离改造方案](../architecture/14-demo-mode-isolated-migration-plan.md)。
> 状态标记：`PASS` / `FAIL` / `BLOCKED` / `SKIPPED`。未执行不得默认为通过；每项必须保留命令、环境、证据、影响和建议。
> 安全要求：本清单不得记录 Bot Secret、企业 Secret、Token、EncodingAESKey、手机号、完整 opaque actor 或联系方式原文；证据中只允许脱敏值、HMAC digest 或短摘要。

## 执行元数据

- 分支/提交：`codex/unified-listing-flow-architecture`；当前 HEAD `9ae8946`（已推送远端）。应用/Worker/AIBot 镜像已按该提交重建。
- 验收日期（含时区）：2026-09-04（Asia/Shanghai）。
- 执行环境：WSL Ubuntu 24.04、Python 3.12、`backend/.venv-wsl`、Docker、MySQL、Redis。
- 企微环境：独立测试企业、测试 Bot；不使用生产企业和生产凭证。
- 测试数据：使用带 `demo_id` 的合成岗位、简历和虚构联系方式；不得混用真实业务数据。
- 证据目录：`.codex-tmp/verification-demo-<date>/`；日志脱敏后再保存。
- 负责人/复核人：master 调度会话 / 独立最终审查会话。

## 本轮执行结果（2026-09-04）

- [x] WSL Ubuntu 24.04、Docker Compose、app、Worker、AIBot connector、MySQL、Redis、nginx 已启动；容器 restart count 均为 `0`。
- [x] `/health` 返回 `status=ok`；`/ready` 返回 `status=ready` 且数据库正常；`/admin/` 返回 HTTP 200。
- [x] Phase17/18/19 数据库迁移已验证；Phase19 首次执行和重复执行均成功，主键为 `(stat_date, target_type, target_id, scope_key)`。
- [x] Phase19 冲突场景已验证 fail-closed：返回 SQLSTATE `45000`，原始数据保留，未执行主键替换。
- [x] 历史全量单元测试：`2671 passed`；演示/推荐/Worker/Cleanup 专项：`777 passed`。
- [x] 独立最终审查通过：无新增 P0/P1 阻断问题；`compileall`、`git diff --check` 均通过。
- [x] 代码已推送至远端分支 `codex/unified-listing-flow-architecture`。
- [x] 真实测试企业 Golden Flow（GF1-GF4）已完成在线验证：厂家发布/补充岗位、求职者搜索、中介双方向搜索、翻页、薪资追问均出现 `inbound done` 与 `outbound sent`；推荐 delivery 已 sent/completed。
- [x] 最近求职者搜索复验：入站事件 78（内容为苏州电子厂求职请求）已 `done`；Worker `replies=1`、`send_ok=true`；对应出站记录 43 为 `sent`；推荐 delivery 已 `completed`，实际返回 3 条。
- [x] 已提交 `9ae8946` 修复 demo turn 的 session key 贯穿 Router/Worker；新增真实 AIBot demo 搜索与厂家发布回归，避免 `one worker turn cannot stage multiple users`。
- [ ] BLOCKED GF5-GF10：第二企微账号授权/撤销、后台禁用、preview/cleanup/replay、connector/Worker 重启接管、ACK 丢失/Redis 短暂不可用，以及生产配置副本 fail-closed 尚未完成在线演练；不得用自动化/mock 结果替代。
- [x] 旧的 `multiple users` dead-letter 记录保留为修复前历史证据，未伪装成已重试成功；修复后的新事件单独核对 `done/sent`。

## 增量执行结果（2026-09-05）

- [x] PASS 首次 `enter_chat` 自我介绍已在提交 `f345b04` 实现：仅当 `DEMO_MODE_ENABLED=true` 且回调 `aibot_id` 命中 `DEMO_ALLOWED_BOT_IDS` 时展示演示欢迎语；演示关闭或 Bot 未命中 allowlist 时保持原通用欢迎语。
- [x] PASS 演示欢迎语覆盖 `/演示`、`/演示 求职者`、`/演示 厂家`、`/演示 中介`，并提示切换角色后可直接描述需求；正文 UTF-8 长度为 `331 bytes`，低于企微文本限制。
- [x] PASS 独立代码审查和 AIBot 协议相邻回归完成：`55 passed`，无阻断性问题；durable acceptance、重复 `enter_chat` 欢迎重放和 5 秒响应截止语义保持不变。

## 0. 硬门禁与停止条件

- [ ] BLOCKED/ PASS 0.1 已确认测试企业、测试 Bot、身份应用和管理员权限；凭证通过环境变量或密钥存储注入，未写入仓库。
- [ ] PASS 0.2 已确认本轮使用单聊；群聊仅验证协议和 fail-closed，不得作为演示业务通过条件。
- [ ] PASS 0.3 已确认 `User.role` 仍只有 `worker / factory / broker`；没有新增业务超级角色，也没有把 `AdminUser.super_admin` 混入业务用户。
- [ ] PASS 0.4 已确认演示模式默认关闭；生产配置即使误设 `DEMO_MODE_ENABLED=true` 也必须启动失败或运行时强制拒绝。
- [ ] BLOCKED 0.5 若出现 opaque actor 写入真实 `User.external_userid`、演示消息发给 synthetic userid、演示查询返回真实岗位/简历/联系方式、清理影响真实数据，立即停止验收并回滚演示开关。

## A. WSL 与服务启动

- [ ] PASS A1 在 WSL 中进入仓库并确认版本：`cd /mnt/d/work/JobBridge && git status --short && git rev-parse --short HEAD`；记录工作区是否有未提交改动。
- [ ] PASS A2 检查配置模板和敏感配置来源：`rg -n "DEMO_MODE|WECOM_AIBOT|DB_URL|REDIS" .env.example backend/.env docker-compose*.yml`；输出不得包含 Secret 值。
- [ ] PASS A3 校验 Compose 配置：`docker compose -f docker-compose.yml -f docker-compose.demo.yml config`；确认 app、worker、mysql、redis、nginx、aibot-connection 的依赖和网络正确。
- [ ] PASS A4 启动/重建测试服务：`docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build`；只使用测试项目和测试数据库。
- [ ] PASS A5 检查容器状态：`docker compose ps`；要求 app、worker、AIBot connector、MySQL、Redis 为 healthy/running，connector 副本数为 1。
- [ ] PASS A6 检查应用和后台健康：`curl -fsS http://127.0.0.1/health`、`curl -fsS http://127.0.0.1/ready`、`curl -fsS -o /dev/null -w '%{http_code}' http://127.0.0.1/admin/`。
- [ ] PASS A7 检查数据库和 Redis 连通性：使用只读探针查询 MySQL 版本、`SELECT 1`，以及 Redis `PING`；证据不保存连接串中的密码。
- [ ] PASS A8 检查日志脱敏：`docker compose logs --no-color --since 5m app worker aibot-connection`；确认不出现 Secret、Token、AESKey、手机号和完整 actor。

## B. 数据库迁移与控制面

- [ ] PASS B1 备份测试数据库并记录备份文件校验值；备份文件不得提交仓库。
- [ ] PASS B2 在隔离测试库执行 `backend/sql/migrations/phase17_001_demo_control_plane.sql`；记录 migration 文件 SHA256 和执行结果。
- [ ] PASS B3 重复执行 Phase 17 up migration；要求幂等，不重复创建表/索引，不修改既有 User、AIBot identity 和业务数据。
- [ ] PASS B4 用 `information_schema` 核对 `demo_workspace`、`demo_workspace_member`、`demo_principal`、`demo_resource` 的列、索引、唯一约束和外键。
- [ ] PASS B5 核对 workspace 一个角色一个 principal：`worker`、`factory`、`broker` 均存在，synthetic userid 唯一且不等于真实企微 userid。
- [ ] PASS B6 验证 workspace 状态机只允许受控转换：`active -> disabled -> cleaning -> cleaned`；失败进入 `failed`，不得自动恢复为 active。
- [ ] PASS B7 静态审查 migration：不存在修改 User.role、关闭外键检查、按前缀盲删、写入明文 actor/secret 的语句。
- [ ] BLOCKED B8 仅在空测试控制面、已导出证据、停止演示流量且所有 demo resource 为 cleaned 时手工验证 guarded down migration；不得对共享业务库执行 down。

## C. 演示配置与生产 fail-closed

- [ ] PASS C1 默认配置核对：`DEMO_MODE_ENABLED=false`、bot allowlist 为空或仅测试值、演示 session TTL 为正数、active workspace 上限为正数。
- [ ] PASS C2 在 development/test 中配置测试 Bot allowlist 并启动；配置解析通过，演示仍需 workspace membership 才能进入业务流程。
- [ ] PASS C3 在 development/test 开启演示但不配置 `DEMO_ALLOWED_BOT_IDS`；启动或配置校验明确失败。
- [ ] PASS C4 在 production 设置 `DEMO_MODE_ENABLED=true`；启动校验失败或运行时始终拒绝，不能通过 actor allowlist 绕过。
- [ ] PASS C5 使用未 allowlist 的 bot_id 请求演示命令；返回确定性拒绝，不建 workspace、不建 principal、不写业务资源。
- [ ] PASS C6 使用已 allowlist actor digest 但没有 workspace membership；请求仍被拒绝，证明静态 allowlist 不能直接代替 workspace 授权。
- [ ] PASS C7 群聊输入演示命令；返回拒绝，不创建 active pointer，不加载演示 session，不执行岗位/简历/Contact 业务。

## D. 企业微信 AIBot 长连接与身份链路

- [ ] PASS D1 connector 使用 `wss://openws.work.weixin.qq.com`，通过 `aibot_subscribe` 发送 bot_id、Secret 和客户端 req_id；日志只记录 req_id 摘要。
- [ ] PASS D2 subscribe ACK 的 req_id 匹配；错误、超时和 TLS 失败进入有界重连，不产生业务入站事件。
- [ ] PASS D3 同一 bot 启动第二个 connector；旧连接被平台踢出或租约失效，新连接接管；无双主 Reader/Writer。
- [ ] PASS D4 heartbeat/ping、断线重连、指数退避和 lease fencing 通过；失去 lease 后旧 connector 立即停止收发。
- [ ] PASS D5 收到 AIBot 单聊 text callback；解析 `schema_version=2`、`source_channel=wecom_aibot`、`conversation_type=single`、`conversation_id`、`ordering_key`、`provider_req_id`。
- [ ] PASS D6 对重复 `(source_channel, provider_msg_id)` callback 重放；只保留一条 durable inbound event，不重复入队、不重复 welcome、不重复业务副作用。
- [ ] PASS D7 在 DB 或 Redis 不可用时收到 callback；不得在持久化/入队前成功确认，不得调用 LLM、Router 或写业务数据。
- [ ] PASS D8 plain userid 只在格式和同企业目录可见性均确认后标记 verified；目录不可见、超时或权限不足均 fail-closed。
- [ ] PASS D9 opaque/open_userid 先进入 AIBot identity 层；未 verified 时只允许欢迎、帮助和绑定引导，不进入真实 User、Session、Action、Contact 或 PII。
- [ ] PASS D10 open_userid 转换成功后，canonical userid、binding、registration 在同一事务内幂等落库；转换失败不产生半个 User 或角色授权。
- [ ] PASS D11 AIBot Bot Secret、身份自建应用 Secret、legacy 企微凭证三者隔离；故意替换其中一个，错误应明确且不回退到另一凭证。
- [ ] PASS D12 入站/出站/审计日志只记录 actor digest、bot_id 脱敏值、provider msgid 摘要、reason_code 和 trace_id，不记录 opaque actor 明文。
- [x] PASS D13 首次 `enter_chat` 在持久化接收成功后选择欢迎语：demo enabled 且 Bot 命中 allowlist 时返回演示自我介绍，否则返回通用欢迎语；重复事件与 5 秒 deadline 行为不变。

## E. 演示工作区创建与多企微账号快速授权

- [ ] PASS E1 由受控管理入口创建 workspace；一次事务内生成 workspace、三个 synthetic principal、初始成员，失败时整体回滚。
- [ ] PASS E2 创建后核对：真实企微账号对应的 canonical User.role 保持原值；三个 synthetic user 分别为 worker/factory/broker，扩展字段带 demo_id 和 synthetic 标记。
- [ ] PASS E3 使用 bot_id + 初始 actor digest 查询 workspace；可定位到正确 workspace，不返回 secret 或 actor 明文。
- [ ] PASS E4 为第二个企微账号授权时只提交其 actor digest、bot_id、demo_id、授权人和可选过期时间；不得要求修改 User.role 或新建真实业务角色。
- [ ] PASS E5 第二个账号授权后立即用同一 digest 解析 workspace membership；不需要重启 app、Worker 或 connector。
- [ ] PASS E6 重复授权同一 workspace/bot/digest；要求幂等，不新增重复 membership，不改变既有 workspace 和 principal。
- [ ] PASS E7 对错误 bot_id、错误 demo_id、格式非法 digest、已清理 workspace 授权；全部确定性拒绝，无数据写入。
- [ ] PASS E8 对授权成员设置过去的 expires_at；首次请求将成员置为 expired 并拒绝进入，不允许过期成员继续使用旧 active pointer。
- [ ] PASS E9 撤销第二个账号：按 workspace + bot_id + actor digest 执行 revoke；撤销幂等，立即阻断后续演示命令和业务请求。
- [ ] PASS E10 验证快速授权审计：记录 workspace、bot_id 脱敏值、actor digest、授权/撤销操作者、时间、结果和 reason_code；不记录 secret、Token 或 actor 原文。
- [ ] PASS E11 授权后验证第二账号不能看到第一账号真实身份、真实业务资源或联系方式；成员资格只赋予 workspace 范围，不赋予平台全局权限。

## F. 同一企微账号三角色切换

- [ ] PASS F1 使用已授权企微账号发送精确命令 `/演示`；返回角色使用提示，不调用 LLM，不改变真实 User.role。
- [ ] PASS F2 发送 `/演示 求职者`；active pointer 指向 worker principal，context 同时带 real_actor_userid、effective_userid、demo_id、bot_id 和 active_role。
- [ ] PASS F3 发送自然语言“我是厂家/我是中介”；不得自动切换角色，仍保持当前角色或进入普通业务澄清。
- [ ] PASS F4 发送 `/演示 厂家`；切换到 factory synthetic principal；原 worker session 的搜索条件、候选快照、草稿和 pending action 不被复用。
- [ ] PASS F5 发送 `/演示 中介`；切换到 broker synthetic principal；broker direction 初始状态正确，不继承 worker/factory 状态。
- [ ] PASS F6 依次切换 worker -> factory -> broker -> worker；三角色均可恢复自己的专属 session，active pointer 只保存当前角色指针。
- [ ] PASS F7 每次切换的业务写入 owner 都是当前 synthetic userid；企微回复和 Outbox 目标始终是 real_actor_userid，不得出现 demo_* 接收人。
- [ ] PASS F8 发送 `/退出演示`；删除/过期 active pointer，后续普通消息恢复真实账号身份，不删除真实 User、AIBot binding 或 legacy session。
- [ ] PASS F9 未授权账号、被撤销账号、disabled/cleaned workspace 执行全部演示命令；均返回确定性拒绝，不产生业务副作用。
- [ ] PASS F10 Worker 重启、Redis pointer 恢复、AIBot WSS 重连和旧消息重放后，不能恢复到已撤销、已禁用或错误 workspace/role。

## G. 会话、回复和数据隔离

- [ ] PASS G1 核对 Redis active pointer：`demo:active:<real_actor>` 只保存当前指针和必要元数据，不保存 Secret、完整原始消息或联系方式。
- [ ] PASS G2 核对三类 session key：`demo:session:<demo_id>:single:<real_actor>:worker|factory|broker`；三类 key 不互相覆盖。
- [ ] PASS G3 worker 搜索只返回当前 workspace 的演示岗位；factory 搜索只返回当前 workspace 的演示简历；broker 两个方向均限制在当前 workspace。
- [ ] PASS G4 演示搜索零结果、放宽、翻页、候选快照和重复确认各自隔离；切换角色后不能沿用其他角色的 criteria、snapshot 或 pending action。
- [ ] PASS G5 演示上传岗位/简历后，业务 owner 为 synthetic principal，资源登记含 demo_id；真实 User、真实岗位、真实简历行不被修改。
- [ ] PASS G6 演示 Contact、Grant、Delivery、Action、Recommendation 全部限于 demo_id；不得向真实用户解锁或发送联系方式。
- [ ] PASS G7 模拟 AIBot reply、stream、update、主动推送和重试；发送目标使用 real actor，channel 使用 wecom_aibot，不能回退 legacy message/send。
- [ ] PASS G8 模拟 ACK 丢失/超时/uncertain；状态机和人工核对路径正确，不因演示模式重复执行业务 Action 或重复发送。
- [ ] PASS G9 日志、审计、推荐正文、Outbox provider_response、Redis 值执行脱敏检查；无 Secret/token/手机号/简历原文/完整 opaque actor。

## H. 管理后台生命周期

- [ ] PASS H1 operator 可查看 workspace 列表和脱敏成员/主体状态；不能创建、禁用或清理。
- [ ] PASS H2 super_admin 可创建 workspace、授权/撤销成员、查看资源预览和清理状态；所有危险动作写审计。
- [ ] PASS H3 非 super_admin 调用 disable/cleanup/retry；返回权限拒绝，不改变 workspace 状态。
- [ ] PASS H4 workspace detail 显示：demo_id、状态、bot 脱敏值、成员 digest、三个 principal、资源统计、最近错误和清理进度；不显示 secret。
- [ ] PASS H5 点击禁用执行 `active -> disabled`；重复禁用幂等，新的演示命令、业务动作、Outbox 发送均被阻断。
- [ ] PASS H6 disabled workspace 仍可查看和预览；不得直接恢复 active。需要新建 workspace 才能重新开始演示。
- [ ] PASS H7 清理前必须有 preview 数量和二次确认；preview 与实际清理对象一致，不能按 userid 前缀猜测范围。

## I. 下架、清理、失败恢复与回滚

- [ ] PASS I1 禁用后演示岗位/简历按既有 lifecycle 下架；不使用真实用户删除流程，不关闭外键检查。
- [ ] PASS I2 清理顺序符合方案：冻结新消息 -> Action/Contact/Outbox -> 岗位/简历 -> 媒体/target cleanup -> Recommendation -> Contact -> Action/event/log -> Redis -> synthetic principals -> workspace cleaned。
- [ ] PASS I3 清理前记录资源快照和备份；只处理 demo_id 资源，真实 User、AIBot binding、legacy 数据计数不变。
- [ ] PASS I4 清理过程分批执行、可重试、幂等；中途进程终止后从 checkpoint 继续，不重复删除、不恢复 active。
- [ ] PASS I5 模拟推荐、Contact、媒体、Outbox、Redis 某一步失败；workspace 进入 failed，错误码和进度可见，retry 只处理未完成项。
- [ ] PASS I6 清理完成后核对：workspace=cleaned、principal/member 不可重新激活、demo resource 无 active 行、演示 Redis key 和 active pointer 清零。
- [ ] PASS I7 清理完成后继续发送旧演示消息/重放旧 provider msgid；不得产生业务副作用，不得重新激活 workspace。
- [ ] PASS I8 清理完成后新建同名 workspace；获得新的 demo_id、principal userid 和资源范围，旧 workspace 数据不串入。
- [ ] BLOCKED I9 guarded down 仅在测试控制面为空且证据导出后执行；down 后历史 legacy、真实 identity、User 和业务表仍完整。

## J. 历史功能回归

- [ ] PASS J1 运行 legacy 核心 unit：`cd backend && source .venv-wsl/bin/activate && pytest -q tests/unit`；记录完整结果，不因演示新增失败而降级。
- [ ] PASS J2 定向回归求职搜索、Dialogue/Session、放宽、翻页、Action、Contact/PII、岗位/简历发布、推荐和生命周期测试。
- [ ] PASS J3 运行 AIBot 专项：callback、client、transport、connection lifecycle、event ACK、group contract、media lifecycle、reply window、stale recovery、rollback guard。
- [ ] PASS J4 运行身份绑定专项：identity client、identity gate、identity service、binding uniqueness、registration、预注册/邀请/审核/撤销 API。
- [ ] PASS J5 运行 mock-testbed backend 和 HTTP/SSE smoke；确认旧的三个模拟用户测试台仍可启动、复用主 Worker 且不依赖真实企微演示控制面。
- [ ] PASS J6 验证 legacy `/webhook/wecom` 与 AIBot channel 并行；legacy 事件不进入 demo workspace，AIBot 演示事件不走 legacy sender。
- [ ] PASS J7 验证现有管理后台登录、RBAC、岗位/简历审核、清理任务、报表和配置页面；演示功能下架不影响原页面。
- [ ] PASS J8 运行 `compileall`、`git diff --check`、migration 静态检查和敏感信息扫描；失败项必须解释并修复或标记 BLOCKED。

## K. 真实企微端到端 Golden Flows

每条 flow 必须记录：企微账号脱敏标识、bot_id 脱敏值、demo_id、输入、逐条回复、inbound/outbox/event 状态、active role、effective userid 摘要、Redis key、资源数量和清理结果。

- [x] PASS GF1 首次真实单聊：AIBot callback -> identity resolve -> verified/binding -> 欢迎/帮助；未把 opaque actor 当明文 userid。
- [x] PASS GF2 已授权账号进入 worker，搜索当前 workspace 岗位并收到真实回复；最近一次复验为 inbound 78 `done`、Worker `replies=1`、outbound 43 `sent`、recommendation delivery `completed`，实际 3 条。
- [x] PASS GF3 同一账号切换 factory，完成岗位发布/补充岗位；岗位写入、补充轮次和出站消息均已完成，未复用 worker session。
- [x] PASS GF4 同一账号切换 broker，依次执行 `/找工人`、`/找岗位`、搜索、翻页和薪资追问；各方向均有 `inbound done` + `outbound sent`，结果仍限于 workspace。
- [ ] BLOCKED GF5 第二企微账号通过快速授权加入同一 workspace；当前没有真实企微在线授权证据（自动化授权测试不替代在线 E2E）。
- [ ] BLOCKED GF6 撤销第二账号后立即发消息；当前没有真实企微在线撤销证据（自动化撤销测试不替代在线 E2E）。
- [ ] BLOCKED GF7 管理后台禁用 workspace；当前没有真实企微在线禁用后消息证据（后台自动化测试不替代在线 E2E）。
- [ ] BLOCKED GF8 执行 preview -> cleanup；当前没有真实企微在线 cleanup/replay 证据（清理专项测试不替代在线 E2E）。
- [ ] BLOCKED GF9 重启 connector/Worker、模拟 ACK 丢失和 Redis 短暂不可用；恢复后不重复业务执行，不向 synthetic userid 发消息。
- [ ] BLOCKED GF10 在生产配置副本验证演示开关 fail-closed；不连接真实生产 Bot，不执行生产写入。

## L. 命令与证据索引

| 编号 | 命令/入口 | 环境 | 结果 | 证据路径/摘要 | 影响/建议 |
|---|---|---|---|---|---|
| A4 | `docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build` | WSL/Docker | PASS | compose 重建后全部服务 running/healthy | 记录镜像与服务版本 |
| B2 | Phase 17 demo control-plane migration | 隔离 MySQL | PASS | Phase17/18/19 已执行并完成 schema 对账 | 记录 SHA256、表/索引对账 |
| C4 | production + DEMO_MODE_ENABLED=true 启动校验 | 配置副本 | PASS（自动化） | fail-closed 专项通过 | 必须 fail-closed |
| D1-D7 | AIBot callback/transport/connection 专项 pytest | WSL .venv-wsl | PASS（自动化） | 长连接/消息链路专项通过 | DB-backed/真实 WSS 分开标记 |
| D13 | 首次 `enter_chat` 演示自我介绍专项 | 独立审查/测试环境 | PASS（自动化） | `f345b04`；演示/非演示/allowlist 边界及协议相邻回归 `55 passed`；正文 UTF-8 `331 bytes` | 真实测试 Bot 下次新会话时补充可视化截图，不记录 Bot 或账号敏感标识 |
| E4-E10 | 管理授权/撤销或服务契约测试 | 隔离 MySQL/Redis | PASS（自动化） | 身份/授权/撤销专项通过 | 只记录 digest，不记录 actor 原文 |
| F1-F10 | 企微单聊命令与 Redis/Outbox 对账 | 测试企业 | PASS（自动化） | 三角色与 session 隔离专项通过 | 记录三角色 session 隔离 |
| H5-H7 | 后台 disable/preview | 测试后台 | PASS（自动化） | 后台生命周期专项通过 | 记录 RBAC 和审计 |
| I2-I8 | cleanup runner/retry/replay | 隔离 MySQL/Redis | PASS（自动化） | cleanup/retry/replay 专项通过 | 记录 checkpoint、孤立行和 key |
| J1-J8 | 历史回归集合 | WSL .venv-wsl | PASS | `2671 passed`，静态校验通过 | 不得用 demo 通过掩盖历史失败 |
| GF1-GF4 | 真实企微 E2E | 测试企业 | PASS | 厂家发布/补充、求职者搜索、中介双方向/翻页/追问均有 inbound `done` + outbound `sent`；最近求职者流为事件 78、出站 43、推荐 delivery `completed`、3 条 | 保留脱敏事件/Outbox/Delivery 对账；旧 dead-letter 仅作为修复前历史证据 |
| GF5-GF10 | 真实企微 E2E | 测试企业/生产配置副本 | BLOCKED | 尚无第二账号授权/撤销、后台禁用/清理重放、重启接管、ACK/Redis 故障在线演练和生产副本门禁证据 | 不得用 unit/mock 结果替代；生产 Bot 不接入 |

## 完成判定

- GF1-GF4 已在测试企业真实在线验证；GF5-GF10 尚缺真实在线证据，未完成前不得宣称全部 Golden Flow 结项。
- 同一企微账号三角色体验通过，且真实 User.role、真实 AIBot binding、legacy channel 不变。
- 第二及后续企微账号可以按 bot_id + actor digest 快速授权和撤销；授权不修改真实业务角色，撤销即时生效。
- 演示数据可预览、下架、分批清理、失败重试和重建；清理不影响真实数据，旧消息重放无副作用。
- 生产环境演示模式保持关闭并 fail-closed；测试企业真实 WSS 已有脱敏对账，明文或加密 userid、重启接管和生产副本门禁仍分别记录，不以 mock/unit 结果替代。
- 最终报告必须附命令、版本、脱敏 DB/Redis 对账、审计摘要、失败影响、回滚结果和复核人签字。

## 证据与敏感信息处理

1. 保存日志前过滤 Secret、Token、EncodingAESKey、手机号、联系方式、简历原文和完整 actor。
2. actor 只保存 HMAC digest 或固定长度摘要；bot_id、provider msgid、trace_id 按项目约定脱敏。
3. 数据库证据优先保存计数、状态、类型、digest 和 resource_id，不导出业务正文。
4. 清理测试完成后删除临时凭证、测试数据库备份中的敏感值和本地 WSS 抓包；保留脱敏报告与校验值。
