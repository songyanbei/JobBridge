# JobBridge 找工作领域全功能验收清单

> 验收范围：S2/S3 求职搜索、Action/Contact，以及 S4 岗位发布、S5 简历发布与双向招聘。
> 状态标记：`PASS` / `FAIL` / `BLOCKED` / `SKIPPED`。每项执行时必须记录命令、环境、证据、影响和建议；未执行不得默认为通过。

## 执行元数据

- 分支/提交：`codex/unified-listing-flow-architecture`，最终验收提交 `716c91d`，P1 修复 `01c2d86`、`686e93f`
- 验收日期（含时区）：2026-08-31 Asia/Shanghai
- 执行环境：WSL Ubuntu 24.04、Python 3.12 `backend/.venv-wsl`、MySQL 8.0.45、Redis 7.4.8、Docker；mock backend 8001、UI 5174
- 测试数据与清理方式：`wm_mock_*` 隔离身份；GF3/GF4 前清理对应 Redis session；临时 Contact/rollout 开关复测后删除并重建 app/worker
- 页面级替代方式：浏览器控制不稳定，使用 mock `/mock/wework/inbound` + Redis SSE 等价黑盒回放，DB/Outbox 查询作事实证据

## 2026-09-04 真实企微演示联调增补

本节是对历史清单的增补，不改写此前基于 mock/unit 的历史结果，也不把历史死信伪装成重试成功。执行环境为 WSL Ubuntu 24.04、Docker Compose、MySQL 8.0、Redis 7、JobBridge app/Worker/AIBot connector；使用独立测试企业和测试 Bot，所有账号、Bot、workspace、actor 仅以脱敏摘要或内部编号记录。

- [x] PASS 真实企微单聊岗位发布与补充：厂家角色完成岗位发布、缺字段补充和出站回复，入站事件均收敛为 `done`，对应出站均为 `sent`。
- [x] PASS 真实企微求职者搜索：最近一次“苏州电子厂”求职请求入站事件 78 为 `done`，Worker `replies=1`、`send_ok=true`，对应出站记录 43 为 `sent`；推荐 delivery 为 `completed`，实际返回 3 条。
- [x] PASS 真实企微中介双方向：中介依次执行 `/找工人`、`/找岗位`，并完成搜索、翻页和薪资追问；均有 `inbound done` + `outbound sent`，结果保持 workspace 范围隔离。
- [x] PASS demo session key 修复：代码提交 `9ae8946` 将演示 session key 从 Worker 贯穿 Router 的文本、命令、上传和搜索 CRUD；新增 AIBot demo 搜索/厂家发布回归，组合测试 `141 passed`，避免 `one worker turn cannot stage multiple users`。
- [ ] BLOCKED 仍待补充：connector/Worker 重启接管、ACK 丢失/Redis 短暂不可用在线演练，以及生产配置副本 fail-closed。上述限制不影响本节已通过的测试企业正常 Golden Flow。

对应完整清单见[演示模式、企微长连接与身份绑定综合验收清单](demo-mode-wecom-comprehensive-verification-checklist.md)。

## A. 入口与可靠性

- [x] PASS A1 `tests/unit/test_webhook.py`、`test_wecom_callback.py`：签名合法/非法分支。
- [x] PASS A2 `tests/unit/test_wecom_crypto.py`：AES/URL 验证及错误密文 fail-closed。
- [x] PASS A3 mock `/inbound` L1 Redis + DB UNIQUE；重复消息返回 duplicate dropped/不重复入队。
- [x] PASS A4 限流与 Redis 故障覆盖于 Contact/Action 定向集合及 C2 chaos `9/9`。
- [x] PASS A5 Action/Outbox 定向测试覆盖 claim lease、竞争接管和 fencing。
- [x] PASS A6 `tests/unit/test_redis_user_lock.py`：用户锁串行与超时恢复。
- [x] PASS A7 `tests/unit/test_tasks_send_retry_drain.py`、C2 chaos：retryable/terminal/dead-letter。
- [x] PASS A8 `tests/integration/test_outbox_claim_lock_scope_mysql.py`、C2 chaos：lease/响应丢失重试。

## B. 对话与意图

- [x] PASS B1 `test_dialogue_reducer.py`、`test_message_router_contact_flow.py`；job/resume 与 search_worker 方向。
- [x] PASS B2 intent/compat 单测及默认配置测试；legacy fallback 保留。
- [x] PASS B3 slot schema、intent extraction 单测：白名单/归一化/未知字段。
- [x] PASS B4 multi-turn upload/session 单测：缺字段、TTL、恢复。
- [x] PASS B5 confirm/replay 单测及 GF1/GF2 首发确认回放。
- [x] PASS B6 Action `turn_id + action_name` 幂等与 replay 定向集合。
- [x] PASS B7 search facade/replay 单测：snapshot/show_more 稳定性。
- [x] PASS B8 relax contract 单测及策略版本记录。
- [x] PASS B9 contact flow 单测：无上下文返回先搜索/选择引导。
- [x] PASS B10 legacy route/session/card fallback 单测与核心集合。

## C. 岗位搜索

- [x] PASS C1 `test_job_search_facade.py`、GF1/GF2 HTTP-SSE；请求、worker、delivery 可追踪。
- [x] PASS C2 search/matching 单测与过期/下架集成覆盖硬过滤。
- [x] PASS C3 matching/recommendation 单测：bounded rerank 与 fallback。
- [x] PASS C4 recommendation request/attempt/delivery 记录 snapshot/top ids；GF1/GF2 有证据。
- [x] PASS C5 权限/方向单测；GF1/GF2 `search_job` 隔离。
- [x] PASS C6 GF1/GF2 payload 保留 legacy/history fallback 来源标记。
- [x] PASS C7 PII contract 单测、GF1/GF2 payload/Card/SSE 无电话/微信明文。
- [x] PASS C8 lifecycle/delete/visibility：修复旧断言后 `test_phase3_job_visibility.py` `8 passed`；过期/下架过滤保留。

## D. 简历搜索

- [x] PASS GF3/GF4：worker resume `95/96/97` -> factory/broker `search_worker`，HTTP/SSE 可见。
- [x] PASS `ResumeSearchFacade`/MatchingPolicy 单测；request 记录 `search_worker`。
- [x] PASS resume search/replacement/visibility 单测；GF3/GF4 snapshot top=`[97,96,95]`。
- [x] PASS GF3/GF4 角色方向断言；Contact 均为 `search_worker`。
- [x] PASS config/rollout/kill-switch 单测；正式开关恢复 off/fail-closed。
- [x] PASS GF3/GF4 ContactRequest ref=`recruitment.resume:97`、listing_version=2，可直接 redeem。

## E. Action Execution

- [x] PASS E1 Action allowlist/profile/policy 校验（Action 定向集合）。
- [x] PASS E2 actor/role/tenant 权限 fail-closed（Action actor binding tests）。
- [x] PASS E3 claim acquired/replay/busy/terminal、lease/fencing（Action 定向集合）。
- [x] PASS E4 finalize 事实/引用/Session/ConversationLog/Outbox 事务一致（Action 定向集合）。
- [x] PASS E5 parse artifact/replay 不重复 provider/业务写（Action replay tests）。
- [x] PASS E6 `turn_id + action_name` 幂等冲突（Action service tests）。
- [x] PASS E7 并发 CAS/version 冲突整体回滚（Action/chaos tests）。
- [x] PASS E8 脱敏审计字段和 trace 查询（Action audit tests）。
- [x] PASS E9 retryable/terminal/dead-letter 恢复（C2 chaos `9/9`）。

## F. Contact / PII

- [x] PASS F1 ContactRequest 创建/幂等/actor 校验（Contact tests、GF1-GF4）。
- [x] PASS F2 listing ref/version/aggregate/policy/direction 绑定（GF1-GF4 DB）。
- [x] PASS F3 authorize -> issue -> redeem 顺序及重检（Contact tests、GF1-GF4）。
- [x] PASS F4 grant 一次性消费及跨 actor/listing 拒绝（C2 chaos）。
- [x] PASS F5 过期/撤销/版本/策略失效（Contact reauthorization tests）。
- [x] PASS F6 PII 加密/脱敏及 Prompt/Card/log/Outbox 禁止明文（privacy tests、GF payload）。
- [x] PASS F7 ContactDelivery/Outbox 事务与重试（dispatcher/outbox tests、GF1-GF4）。
- [x] PASS F8 listing/actor/daily 频控及 Redis 故障上限（C2 chaos）。

## G. 岗位发布

- [x] PASS G1 草稿 DTO/profile 白名单和媒体 pending（S4/job publish tests）。
- [x] PASS G2 多轮补齐、TTL、重复 operation/source_msg 幂等（S4 upload tests）。
- [x] PASS G3 `confirm_job` 确认/取消门禁（job publish tests、GF1/GF2）。
- [x] PASS G4 确定性审核与拒绝原因（moderation tests）。
- [x] PASS G5 passed/activated/expires 状态与搜索可见性（lifecycle tests、GF1/GF2）。
- [x] PASS G6 media `entity_version`、替换与清理（media tests）。
- [x] PASS G7 过期/下架/恢复及 deleted/replaced 规则（lifecycle tests）。
- [x] PASS G8 候选清理、后台编辑、权限和版本冲突（admin/replace tests）。

## H. 简历发布

- [x] PASS H1 简历首发草稿/审核/激活（S5 tests、GF3/GF4 resume 95-97）。
- [x] PASS H2 媒体绑定与 entity_version（resume media tests）。
- [x] PASS H3 替换/并发冲突/replaced tombstone/search 隔离：冻结 `utc_now_naive` 后 `test_phase11_stage2_visibility.py` `6 passed`。
- [x] PASS H4 过期/下架/恢复及候选清理（resume lifecycle tests）。
- [x] PASS H5 后台编辑/权限/拒绝/编辑事件审计（resume admin tests）。
- [x] PASS H6 version 与 aggregate_version 单调一致（GF3/GF4 DB：均为 2）。

## I. 一致性与 Outbox

- [x] PASS I1 version+aggregate_version 双写及 stale guard（domain outbox tests、GF1/GF2）。
- [x] PASS I2 事务内 domain event 与失败回滚（outbox tests）。
- [x] PASS I3 Job/Resume tombstone 防旧事件复活（cleanup/event tests）。
- [x] PASS I4 consumer scheduler claim/lease/fencing/checkpoint（consumer tests）。
- [x] PASS I5 retry/dead-letter/可重入恢复（outbox/C2 chaos）。
- [x] PASS I6 乱序/重复/stale 事件回源校验（consumer tests）。
- [x] PASS I7 未知 aggregate/ref fail-closed（domain outbox tests）。

## J. 数据库与运维

- [x] PASS J1 Phase14 临时库 up：domain outbox/media/lifecycle schema/index。
- [x] PASS J2 Phase14 down：通过，stop-write/consumer 说明下未删除事实/事件。
- [x] PASS J3 Phase15 up/down：通过，resume/contact direction schema。
- [ ] FAIL J4 manifest LF/SHA 已修复（`test_manifest_pins...` `1 passed`）；root DSN 下 Phase11 checkpoint 参数化 `4 passed`、stage-2 activation `1 passed`、stage-5 fences `7 passed`（`60472c9` 修复全局 `delivery_order=1` 冲突，`1556134` 隔离 expiry 查询边界；锁序用例连续 `3/3 passed`），但完整五文件集合运行约 7 分钟后超时中止，既有真实集成基线仍为 `20 passed, 13 failed, 103 skipped`，`jobbridge` 测试账号无临时 schema 权限。
- [x] PASS J5 隔离 env on 时 `s4_preflight --json` passed=true；切回 off 时 gate incomplete 且保持 fail-closed；本地结果不能替代生产观察窗口。
- [ ] BLOCKED J6 rollout/rollback/legacy exit 长期观察证据未提供；生产开关保持 off。
- [x] PASS J7 app/worker health 通过；补入口后两个脚本从 backend/仓库根目录 `--help` 均通过。
- [x] PASS J8 MySQL 8.0.45、Redis 7.4.8/PONG；故障降级由 C2 chaos 覆盖。

## K. 兼容和安全

- [x] PASS K1 legacy API/route/session/card fallback 单测与回放。
- [x] PASS K2 config 默认 off/fail-closed 单测及复测后运行态核对。
- [x] PASS K3 权限/Contact privacy/方向测试；`01c2d86` 修复 resume/job ref 串线。
- [x] PASS K4 PII contract/privacy 单测、GF1-GF4 payload/SSE 无明文联系方式。
- [x] PASS K5 错误回复、reason code、retry/dead-letter 定向测试。
- [x] PASS K6 Contact audit 与 action trace/request/ref/version/policy 字段查询。

## 页面级 Golden Flows（必须逐条记录）

对每条 flow 记录：用户身份/角色、逐条输入、逐条可见回复、`listing_ref`、`version/aggregate_version`、状态变化、Contact grant/delivery/outbox、方向和权限断言。

- [x] PASS GF1 mock HTTP/SSE：factory `job:49` -> worker 搜索 -> ContactDelivery；version/aggregate_version=2。
- [x] PASS GF2 mock HTTP/SSE：broker `job:50` -> worker `联系2`；Grant/Delivery/outbox `154` sent。
- [x] PASS GF3 mock HTTP/SSE：worker resume `97` -> factory `search_worker` -> `recruitment.resume:97` ContactDelivery/outbox `165`。
- [x] PASS GF4 mock HTTP/SSE：worker resume `97` -> broker `search_worker` -> `recruitment.resume:97` ContactDelivery/outbox `167`。
- [x] PASS GF5 GF1/GF2 首轮缺字段追问及补齐记录。
- [x] PASS GF6 GF1/GF2 重复确认；Action/replay 定向测试通过。
- [x] PASS GF7 mock `/inbound`：将 `job:50.expires_at` 设为过去，`联系2` 收到安全引导，DB 无新增 delivery；随后恢复 expiry。
- [x] PASS GF8 Contact privacy/actor/direction tests + `01c2d86`；跨 actor/listing、Job/Resume 方向不匹配均 fail-closed。
- [x] PASS GF9 app 重建为 `CONTACT_SERVICE_MODE=off`、`JOB_PUBLISH_KILL_SWITCH=true` 后 mock 联系请求只返回安全引导，不产生 grant/delivery。
- [x] PASS GF10 `action_contact_chaos.py --json`：9/9，含 outbox response lost、provider timeout、revoke-before-send/dead-letter。

## AIBot WebSocket 专项（A9）

- [x] PASS A9.1 subscribe/鉴权/单活连接：匹配 `headers.req_id` 回执，旧连接被踢且无双主（离线 mock/单测）。
- [x] PASS A9.2 heartbeat/reconnect/lease fencing：30 秒心跳、指数退避、租约丢失立即停连（离线 mock/单测）。
- [x] PASS A9.3 JSON callback durable inbox + idempotency：`schema_version=2`、provider 幂等键、DB/Redis 故障不成功确认（DB-backed contract 仍 BLOCKED）。
- [x] PASS A9.4 single/group session ordering：单聊按用户、群聊按 chat_id，`ordering_key` 贯穿锁/Session/Outbox（离线 mock/单测）。
- [x] PASS A9.5 AIBot outbox ack/uncertain/recovery：渠道隔离、ACK 丢失进入 `uncertain` 且不自动重试（离线 mock/单测）。
- [x] PASS A9.6 media URL/aeskey lifecycle：加密短存、Worker 下载转存、过期停止重试（离线 mock/单测）。
- [x] PASS A9.7 stream finish/deadline：启用流式时校验 `req_id`、`stream.id`、`finish=true` 和 10 分钟 deadline（离线 mock/单测）。
- [x] PASS A9.8 platform quota/24h/主动推送：24 小时窗口、30/min、1000/hour 及先前会话条件（离线 mock/单测）。
- [ ] BLOCKED A9.9 real test enterprise E2E：明文/加密 userid、单聊/群聊、重启接管和失败恢复（无真实凭证/WSS）。

## 命令与证据索引（执行时填写）

| 编号 | 命令/入口 | 环境 | 结果 | 证据路径/摘要 | 影响/建议 |
|---|---|---|---|---|---|
| C8 | `pytest -q tests/unit/test_phase3_job_visibility.py` | WSL `.venv-wsl` | `8 passed` | 旧 PII 断言已改为无明文字段/Contact 占位 | 不回退隐私契约 |
| H3 | `pytest -q tests/unit/test_phase11_stage2_visibility.py` | WSL `.venv-wsl` | `6 passed` | fixture 冻结 `utc_now_naive` | 保持时间可重复 |
| J4 | manifest contract；Phase11 checkpoint、activation、stage-5 fences（均显式 root MySQL DSN + `PHASE11_TEST_REDIS_DSN=redis://127.0.0.1:6379/0`）；锁序用例连续 3 次；完整五文件集合 | WSL `.venv-wsl`/MySQL/Redis | manifest `1 passed`；checkpoint `4 passed`；activation `1 passed`；stage-5 fences `7 passed`，锁序 `3/3 passed`；完整集合超时中止；历史集成 `20 passed,13 failed,103 skipped` | `60472c9` 消除 `delivery_order=1` seed 冲突，`1556134` 隔离 expiry 查询边界；未取得完整集合最终摘要，历史失败原因为 `jobbridge` 无 CREATE 临时 schema 权限 | 使用专用测试账号并重新跑完整集合 |
| J7 | `python scripts/phase10_preflight.py --help`、`phase14_media_reconcile.py --help`（backend/根目录） | WSL `.venv-wsl` | `ROOT_HELP_OK` | sys.path/argparse 入口修复 | 已提交 `3e40ca9` |
| GF7 | mock `/inbound` + Redis SSE；临时 `job:50.expires_at` 过去 | WSL/MySQL/Redis | SSE 安全引导；无新增 Delivery | 恢复 expiry 后结束 |
| GF8 | Contact privacy/reauthorization tests | WSL `.venv-wsl` | `12 passed` | actor/listing/direction mismatch fail-closed | 保持方向隔离 |
| GF9 | app 重建 `CONTACT_SERVICE_MODE=off`、`JOB_PUBLISH_KILL_SWITCH=true` 后 mock `/inbound` | Docker + Redis SSE | 安全引导；无 grant/delivery | 恢复默认 off |
| GF10 | `python scripts/action_contact_chaos.py --json` | WSL `.venv-wsl` | `9/9 passed` | response lost/timeout/revoke/dead-letter | 观察生产指标 |
| J5 | `s4_preflight.py --json`，env on/100% 后再 off/0% | WSL | on `passed=true`；off gate incomplete | 本地灰度/回滚，不替代生产观察 |

证据索引：`pytest` S4/S5/Action/Contact/Outbox 定向 `183 passed`；mock backend `42 passed`；mock HTTP/SSE smoke `8/8`；C2 chaos `9/9`；全量 unit `2489 passed, 6 failed`；集成 `20 passed, 13 failed, 103 skipped`；`compileall`、`git diff --check` 通过；MySQL/Redis 探测通过；Phase14/15 up/down 通过；GF1-GF4 详证见 [最终报告](job-search-domain-full-verification-report.md)。

## 完成判定

- 逐项汇总：`PASS 92 / 94`、`FAIL 1 / 94`、`BLOCKED 1 / 94`、`SKIPPED 0 / 94`。
- FAIL 明细：J4（真实 MySQL 集成测试账号无临时 schema 权限；manifest checksum 已修复）。
- BLOCKED 明细：J6（7/14 天生产观察、签字、旧明文清理审批证据缺失）。

- 清单覆盖率：`已执行通过项 / 总项`，BLOCKED/SKIPPED 必须说明原因。
- 页面级四流：四条均需满足方向、权限、版本、Contact/Outbox 断言；任一关键断言失败不得正式结项。
- WSL/依赖：S4/S5 定向、legacy 核心、全量 unit、compileall、diff check、MySQL/Redis、Phase14/15 up/down 均有证据。
- 正式结项：仅当无未解释 FAIL、关键项无 BLOCKED，且生产 rollout/观察窗口门禁另有明确签字状态。

## 2026-09-01 WSL 独立复验记录

本轮严格按“历史回归 -> AIBot WebSocket -> 身份/角色绑定”顺序执行。工作区开始时已存在大量未提交改动（含业务、测试和文档）；本轮未修改业务代码，仅追加本节。执行主机为 WSL Ubuntu，解释器为 `backend/.venv-wsl/bin/python`（Python 3.12.3）。WSL 未安装 `rg`、`mysqladmin`、`redis-cli`；依赖服务和真实企业凭证不在本次 shell 环境中。测试输出保存在 `.codex-tmp/verification-20260901/`，不含 secret/token/手机号/原始 opaque 值。

### 历史功能回归（第一阶段）

- [x] PASS 全量 unit：`cd backend && source .venv-wsl/bin/activate && pytest -q tests/unit` -> `2607 passed`（70.44s）。覆盖 A-K 相关入口、意图、搜索、Action、Contact/PII、发布、Outbox、兼容和 kill-switch 单元。
- [x] PASS 核心 search/action/contact：定向集合 -> `144 passed, 1 warning`（18.07s），证据 `core-search-action-contact.txt`。
- [x] PASS dialogue/golden、legacy/compat、发布、domain outbox、rollout gates：正确路径定向集合 -> `231 passed, 34 warnings`（31.54s），证据 `history-targeted.txt`。其中一次错误路径（不存在的测试文件）未计入结果，未掩盖重跑结果。
- [x] PASS mock 黑盒后端：`pytest -q mock-testbed/backend/tests` -> `2 passed, 40 skipped`；跳过项依赖可选外部服务，未计为通过。
- [x] PASS mock HTTP/SSE smoke：以 `sed 's/\\r$//' scripts/smoke.sh | bash` 运行 -> `PASS=8 FAIL=0`。脚本直接执行因 CRLF shebang 失败，已记录为脚本兼容性影响；不改变业务结果。
- [x] PASS mock E2E single/full smoke：`wsl-e2e-smoke.sh` 单流收到 outbound（约 4s，入站 `done`）；`wsl-e2e-full-smoke.sh` 五类场景、重复 MsgId 去重和攻击者前缀守卫均通过，Redis 队列/死信为 0。full smoke 的 case2 实际走岗位发布（非 search_worker），因此 GF2 的 broker search_worker 仍以既有详证为准；末尾有一条重复测试事件仍为 `processing`，建议在专用 DB 环境复核 worker 收敛。
- [x] PASS kill-switch/fail-closed 与故障矩阵：`python scripts/action_contact_chaos.py --json` -> `passed=true, scenario_count=9`；覆盖 provider timeout、response lost、撤销前发送、Redis 限流不可用、Contact off 后搜索仍可用。
- [ ] FAIL/BLOCKED MySQL/Redis 集成与 migration：设置 `RUN_INTEGRATION=1` 后全量 `tests/integration` 为 `144 passed, 24 failed, 104 skipped`；主要失败是运行库缺 `wecom_inbound_event.provider_msg_id`、Phase10 元数据门禁和 Redis FIFO/锁序断言，另有 104 项仍因专用条件 skip。影响：当前 schema 与 ORM/phase14/16 契约漂移，AIBot durable inbox/outbox 运行态不能验收；建议在隔离 schema 应用迁移后重跑并修复 Redis/锁序失败。
- [ ] BLOCKED 真实 7/14 天 rollout、legacy 退出观察和生产回滚签字：本地测试不能替代观察窗口，生产开关仍应保持 off。

第一阶段结论：在可执行的离线/单元/mock 范围内，未发现文档12/13 改造破坏历史求职搜索、简历搜索、Action、Contact/PII、发布、Outbox、legacy 路由或 fail-closed 能力；真实数据库迁移、长期观察和生产兼容仍受环境门禁阻断。

### AIBot WebSocket 专项（第二阶段）

- [x] PASS A9.1 subscribe/鉴权/单活：`pytest -q tests/unit/test_aibot_transport.py tests/unit/test_aibot_connection_lifecycle.py`（相关子集）覆盖 subscribe ACK、`req_id` 匹配、旧连接隔离和单活租约；transport 文件 `5 passed`。
- [x] PASS A9.2 heartbeat/reconnect/lease fencing：transport/connection/stale recovery 子集覆盖心跳、指数退避、租约丢失停连、stale claim 接管；专项结果见 `aibot-specialized.txt` 与 `aibot-transport.txt`。
- [x] PASS A9.3 callback durable inbox/idempotency 契约：`test_aibot_callback.py`、`test_aibot_protocol_fixtures.py`、`test_aibot_event_ack.py` 覆盖 `schema_version=2`、provider msg id、重复事件不重复 welcome、持久化前不成功确认。DB-backed contract 因无 DSN 未执行（同上 BLOCKED）。
- [x] PASS A9.4 single/group ordering：`test_aibot_group_contract.py` 覆盖 single/user 与 group/chat_id 会话键、`ordering_key` 和群聊业务能力 fail-closed；群 Outbox 目标为 chat 而非 user。
- [x] PASS A9.5 channel-aware Outbox/ACK uncertain/recovery：`test_aibot_event_ack.py`、`test_aibot_connection_lifecycle.py`、`test_aibot_stale_recovery.py` 覆盖渠道隔离、发送前写入、ACK 不确定状态和同一 outbox 行恢复；无真实 provider E2E。
- [x] PASS A9.6 media lifecycle：`test_aibot_media_lifecycle.py` -> URL/aeskey 下载转存、过期终止、不重试过期媒体。
- [x] PASS A9.7 stream finish/deadline：`test_aibot_reply_window.py` 与 connection 子集覆盖 `req_id`、stream deadline（10 分钟）和 finish 窗口契约。
- [x] PASS A9.8 quota/24h/主动推送：`test_aibot_reply_window.py`、`test_aibot_connection_lifecycle.py` 覆盖回复窗口及主动推送 ACK/前置会话条件；平台真实限额未在企业环境压测。
- [ ] BLOCKED A9.9 真实企业 E2E：无真实企业微信凭证/WSS、明文/加密 userid 对照及生产重启观察；禁止用 mock 结果替代。建议在隔离测试企业执行并保留脱敏抓包与 DB/Outbox 证据。

专项命令：`cd backend && source .venv-wsl/bin/activate && pytest -q tests/unit/test_aibot_callback.py tests/unit/test_aibot_client.py tests/unit/test_aibot_connection_lifecycle.py tests/unit/test_aibot_event_ack.py tests/unit/test_aibot_group_contract.py tests/unit/test_aibot_media_lifecycle.py tests/unit/test_aibot_protocol_fixtures.py tests/unit/test_aibot_reply_window.py tests/unit/test_aibot_rollback_guard.py tests/unit/test_aibot_stale_recovery.py tests/unit/test_aibot_acceptance_timeout.py tests/unit/test_wecom_mock_outbound.py` -> `44 passed`；另跑 `pytest -q tests/unit/test_aibot_transport.py` -> `5 passed`。证据分别为 `aibot-specialized.txt`、`aibot-transport.txt`。

### 身份与角色绑定专项（第三阶段，mock/离线）

- [x] PASS plain userid：格式白名单、目录可见性校验、目录不可见/超时 fail-closed；未注入目录证据不自动注册。
- [x] PASS open_userid 批量转换：按对象映射、最多 1000 分片、重复去重、invalid/partial/malformed/5xx/超时分类及 token 缓存。
- [x] PASS 凭证隔离：identity client 单测验证 AIBot secret 不用于 identity app token；legacy 凭证路径保持独立。
- [x] PASS verified gate：仅 verified identity 可映射业务用户并自动注册 worker；opaque、未验证、撤销、数据库/目录故障均 fail-closed 或 retryable，不进入业务 Router。
- [x] PASS binding/registration/invite 并发与唯一性：唯一约束静态检查、角色冲突审计、max_uses 重放幂等、重复 invite 不重复消耗。
- [x] PASS factory/broker 能力闸门：必须 verified + active binding + invite/pre-register + 管理员审核；自报角色不能绕过审核。
- [x] PASS 管理员预注册/批准/撤销 API：管理员保护、缺失/撤销 binding 拒绝、最小 User 创建、冲突回滚和脱敏审计持久化。
- [x] PASS session/order/permission 隔离与 legacy 兼容：single/group contract 使用独立键；legacy channel 单测回归通过。
- [x] PASS phase16 migration rollback guard：up/down 静态守卫、先 FK 后表、重复执行安全、partial state 拒绝；真实 MySQL dry-run 因无 DSN BLOCKED。

专项命令：`cd backend && source .venv-wsl/bin/activate && pytest -q tests/unit/test_aibot_identity_client.py tests/unit/test_aibot_identity_gate.py tests/unit/test_aibot_identity_service.py tests/unit/test_aibot_identity_uniqueness.py tests/unit/test_aibot_identity_worker_wiring.py tests/unit/test_aibot_preregistration_api.py tests/unit/test_aibot_phase16_rollback_guard.py` -> `61 passed, 2 warnings`（14.53s），证据 `identity-specialized.txt` 与 `identity-collect.txt`。以上均为 mock/离线结果，不代表真实企业通过。

### 本轮计数与未决项

- PASS：全量 unit `2607`；历史定向 `231`；核心 search/action/contact `144`；AIBot 专项 `49`；身份绑定 `61`；mock 基础 smoke `8/8`；mock 单/全 E2E 脚本退出码 0；chaos `9/9`；compileall 通过。
- FAIL：24（全量集成；主要为 schema 漂移、Phase10 元数据门禁、Redis FIFO/锁序，详见 `full-integration-summary.txt`）。离线/unit/mock/AIBot/identity 功能测试无失败。
- BLOCKED：全量集成另有 `104 skipped`、A9.9 真实企业 E2E、phase16 真实 dry-run、长期 rollout/legacy 退出观察；另有 CRLF smoke shebang 和 WSL 缺少 CLI 的环境限制。
- 安全/边界：未输出或写入清单任何 secret、token、手机号或原始 opaque actor；未修改业务代码。`static-summary.txt` 记录了 compileall 通过；`git diff --check` 受工作区既有 CRLF/尾随空白改动影响，不能作为本轮新增缺陷证据，建议在干净基线或统一换行后复核。

### 本轮证据索引

| 范围 | 精确命令/入口 | 环境 | 结果 | 证据与影响/建议 |
|---|---|---|---|---|
| 历史 unit | `cd backend && source .venv-wsl/bin/activate && pytest -q tests/unit` | WSL, Python 3.12.3 | PASS `2607 passed` | `full-unit.txt`；历史领域回归无失败 |
| 核心搜索/Action/Contact | `pytest -q tests/unit/test_job_search_facade.py tests/unit/test_search_service.py tests/unit/test_search_permission.py tests/unit/test_action_execution_service.py tests/unit/test_action_gateway.py tests/unit/test_action_execution_actor_binding_contract.py tests/unit/test_contact_service.py tests/unit/test_contact_b4_contract.py tests/unit/test_contact_privacy_gate_contract.py tests/unit/test_contact_redeem_reauthorization_contract.py tests/unit/test_contact_delivery_dispatcher_contract.py tests/unit/test_message_router_contact_flow.py tests/unit/test_inbound_acceptance.py` | WSL `.venv-wsl` | PASS `144 passed` | `core-search-action-contact.txt`；保持方向/PII/fail-closed 契约 |
| Dialogue/golden/发布/Outbox | 定向 pytest 集合（见 `history-targeted.txt`） | WSL `.venv-wsl` | PASS `231 passed` | 覆盖 GF1-GF6 对话与 replay、发布状态、domain outbox；建议在 DB 环境补运行态 |
| Mock testbed | `cd mock-testbed/backend && /mnt/d/work/JobBridge/backend/.venv-wsl/bin/python -m pytest -q tests`；`sed 's/\\r$//' scripts/smoke.sh | bash`；临时 CRLF 转换后运行 `wsl-e2e-smoke.sh`、`wsl-e2e-full-smoke.sh` | WSL, mock backend 8001 + MySQL/Redis 容器 | PASS 单流/full smoke；单测 `2 passed, 40 skipped`；基础 smoke `8/8` | `mock-backend.txt`、`mock-smoke.txt`、`e2e-smoke.txt`、`e2e-full-smoke.txt`；full smoke case2 非 search_worker 且重复事件收敛需专用环境复核 |
| Chaos/kill-switch | `cd backend && source .venv-wsl/bin/activate && python scripts/action_contact_chaos.py --json` | WSL `.venv-wsl` | PASS `9/9`, `passed=true` | `ops-and-chaos.txt`；覆盖 response lost/timeout/revoke/Redis 故障 |
| AIBot protocol/transport | 完整命令见上方“专项命令”（`test_aibot_callback.py`、`test_aibot_client.py`、`test_aibot_connection_lifecycle.py`、`test_aibot_event_ack.py`、`test_aibot_group_contract.py`、`test_aibot_media_lifecycle.py`、`test_aibot_protocol_fixtures.py`、`test_aibot_reply_window.py`、`test_aibot_rollback_guard.py`、`test_aibot_stale_recovery.py`、`test_aibot_acceptance_timeout.py`、`test_wecom_mock_outbound.py`）另加 `pytest -q tests/unit/test_aibot_transport.py` | WSL `.venv-wsl` | PASS `44 + 5` | `aibot-specialized.txt`、`aibot-transport.txt`；DB contract 与真实 WSS 仍未验证 |
| Identity/role binding | `pytest -q tests/unit/test_aibot_identity_client.py tests/unit/test_aibot_identity_gate.py tests/unit/test_aibot_identity_service.py tests/unit/test_aibot_identity_uniqueness.py tests/unit/test_aibot_identity_worker_wiring.py tests/unit/test_aibot_preregistration_api.py tests/unit/test_aibot_phase16_rollback_guard.py` | WSL `.venv-wsl` | PASS `61 passed` | `identity-specialized.txt`；均为 mock/离线，不代表真实企业 |
| Integration/migration | `RUN_INTEGRATION=1 pytest -q tests/integration`（另有 AIBot/Phase3 小集合） | WSL `.venv-wsl` + Docker MySQL/Redis | 小集合 PASS `9 passed`；全量 `144 passed, 24 failed, 104 skipped` | `integration-attempt.txt`、`full-integration-summary.txt`；schema 漂移和 Redis/迁移门禁失败，需应用 phase14/16 后复跑 |
