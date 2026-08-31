# JobBridge 找工作领域全功能验收清单

> 验收范围：S2/S3 求职搜索、Action/Contact，以及 S4 岗位发布、S5 简历发布与双向招聘。
> 状态标记：`PASS` / `FAIL` / `BLOCKED` / `SKIPPED`。每项执行时必须记录命令、环境、证据、影响和建议；未执行不得默认为通过。

## 执行元数据

- 分支/提交：`codex/unified-listing-flow-architecture`，最终验收提交 `716c91d`，P1 修复 `01c2d86`、`686e93f`
- 验收日期（含时区）：2026-08-31 Asia/Shanghai
- 执行环境：WSL Ubuntu 24.04、Python 3.12 `backend/.venv-wsl`、MySQL 8.0.45、Redis 7.4.8、Docker；mock backend 8001、UI 5174
- 测试数据与清理方式：`wm_mock_*` 隔离身份；GF3/GF4 前清理对应 Redis session；临时 Contact/rollout 开关复测后删除并重建 app/worker
- 页面级替代方式：浏览器控制不稳定，使用 mock `/mock/wework/inbound` + Redis SSE 等价黑盒回放，DB/Outbox 查询作事实证据

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
- [ ] FAIL C8 lifecycle/delete/visibility：Phase3 job visibility 基线有 3 个全量 unit 失败；过期候选专项通过，需修复/重新基线后复验。

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
- [ ] FAIL H3 替换/并发冲突/replaced tombstone/search 隔离：Phase11 resume visibility 基线有 2 个全量 unit 失败；replacement 定向测试通过，需复验可见性。
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
- [ ] FAIL J4 schema/index 约束存在，但全量 unit manifest checksum 有 1 例失败；真实集成集合为 `20 passed, 13 failed, 103 skipped`（测试账号无临时 schema 权限）。
- [ ] BLOCKED J5 `s4_preflight --json` action/contact gate incomplete；默认 off 正确但放量门禁未满足。
- [ ] BLOCKED J6 rollout/rollback/legacy exit 长期观察证据未提供；生产开关保持 off。
- [ ] FAIL J7 app/worker 启动/health 复测通过，但 `phase10_preflight.py`、`phase14_media_reconcile.py` 直接执行缺少统一模块入口（`ModuleNotFoundError: app`）；运维脚本需修复后复验。
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
- [ ] SKIPPED GF7 未在本轮页面/HTTP flow 专门制造过期/下架后联系拒绝；生命周期单测已覆盖。
- [ ] SKIPPED GF8 未在页面 flow 专门执行跨 actor/跨方向拒绝；Contact 单测已覆盖。
- [ ] SKIPPED GF9 未在页面 flow 专门切换 kill switch；preflight/配置单测已覆盖。
- [ ] SKIPPED GF10 未在页面 flow 专门制造 Outbox dead-letter；C2 chaos/Outbox 单测已覆盖。

## 命令与证据索引（执行时填写）

| 编号 | 命令/入口 | 环境 | 结果 | 证据路径/摘要 | 影响/建议 |
|---|---|---|---|---|---|
|  |  |  |  |  |  |

证据索引：`pytest` S4/S5/Action/Contact/Outbox 定向 `183 passed`；mock backend `42 passed`；mock HTTP/SSE smoke `8/8`；C2 chaos `9/9`；全量 unit `2489 passed, 6 failed`；集成 `20 passed, 13 failed, 103 skipped`；`compileall`、`git diff --check` 通过；MySQL/Redis 探测通过；Phase14/15 up/down 通过；GF1-GF4 详证见 [最终报告](job-search-domain-full-verification-report.md)。

## 完成判定

- 逐项汇总：`PASS 84 / 94`、`FAIL 4 / 94`、`BLOCKED 2 / 94`、`SKIPPED 4 / 94`。
- FAIL 明细：C8（Phase3 可见性 3 例）、H3（Phase11 可见性 2 例）、J4（manifest checksum 及集成权限）、J7（预检脚本入口）。
- BLOCKED 明细：J5（S4/S5 放量门禁仍 off）、J6（rollout/rollback/legacy 观察窗口证据缺失）。

- 清单覆盖率：`已执行通过项 / 总项`，BLOCKED/SKIPPED 必须说明原因。
- 页面级四流：四条均需满足方向、权限、版本、Contact/Outbox 断言；任一关键断言失败不得正式结项。
- WSL/依赖：S4/S5 定向、legacy 核心、全量 unit、compileall、diff check、MySQL/Redis、Phase14/15 up/down 均有证据。
- 正式结项：仅当无未解释 FAIL、关键项无 BLOCKED，且生产 rollout/观察窗口门禁另有明确签字状态。
