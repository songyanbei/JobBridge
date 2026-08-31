# JobBridge 找工作领域全功能验收清单

> 验收范围：S2/S3 求职搜索、Action/Contact，以及 S4 岗位发布、S5 简历发布与双向招聘。
> 状态标记：`PASS` / `FAIL` / `BLOCKED` / `SKIPPED`。每项执行时必须记录命令、环境、证据、影响和建议；未执行不得默认为通过。

## 执行元数据

- 分支/提交：
- 验收日期（含时区）：
- 执行环境：Windows / WSL 发行版、Python、MySQL、Redis、浏览器
- 测试数据与清理方式：
- 页面级替代方式（若无可用页面）：

## A. 入口与可靠性

- [ ] A1 `GET/POST /webhook/wecom` 企微签名校验：合法签名通过，错误签名拒绝。
- [ ] A2 企微消息 AES 解密/URL 验证：合法密文解密，错误 token/ciphertext fail-closed。
- [ ] A3 入站 `msg_id` L1/L2 幂等：重复消息不重复入队、不重复业务写入。
- [ ] A4 入站限流：用户/全局边界、Redis 不可用时保守本地限流，拒绝事件可审计。
- [ ] A5 inbound event claim：lease、竞争 claim、过期接管、fencing token。
- [ ] A6 用户锁：同一用户串行处理，跨用户并行，锁超时可恢复。
- [ ] A7 失败重试：可重试/终态分类、最大尝试次数、错误不吞、dead-letter。
- [ ] A8 出站 Outbox 重试：sending lease 恢复、HTTP 超时/响应丢失不重复业务副作用。

## B. 对话与意图

- [ ] B1 job profile 与 resume profile 入口识别及方向绑定。
- [ ] B2 legacy/v2 intent 路由、v2 失败 legacy fallback、开关默认 fail-closed。
- [ ] B3 字段抽取、归一化、白名单和未知字段丢弃。
- [ ] B4 缺字段多轮补充：追问顺序、上下文持久化、TTL/过期恢复。
- [ ] B5 confirm：摘要/diff、确认/取消/重新编辑、nonce/digest 校验。
- [ ] B6 重复消息/同 `turn_id + action_name` 幂等与 replay。
- [ ] B7 `show_more`：快照/游标延续、重复翻页不重复 rerank/不越权。
- [ ] B8 放宽契约：仅放宽允许字段、明确告知用户、策略版本可追溯。
- [ ] B9 无上下文“联系”引导到先搜索/选择，禁止猜测 listing。
- [ ] B10 legacy fallback 保留历史 Session/Card/API/route 兼容。

## C. 岗位搜索

- [ ] C1 factory/broker 请求经 facade/broker 进入 worker，结果回传可追踪。
- [ ] C2 硬过滤：方向、地区、薪资、工种、状态、有效期/下架。
- [ ] C3 有限重排：白名单字段、单次调用、超时 fallback、不可重复重排。
- [ ] C4 候选快照、`snapshot_id`、分页和 `show_more` 稳定性。
- [ ] C5 factory 与 broker 方向隔离，不能串线或跨租户读取。
- [ ] C6 来源标记保留（factory/broker/legacy/fallback）。
- [ ] C7 搜索 payload/Card/Prompt/日志不含联系人 PII。
- [ ] C8 过期、下架、已替换、删除候选硬过滤；旧快照 fail-closed。

## D. 简历搜索

- [ ] D1 worker 发布的简历经 worker -> factory/broker facade/broker 搜索。
- [ ] D2 `ResumeSearchFacade` 入口与 `MatchingPolicy v1` 版本记录。
- [ ] D3 硬过滤及有限重排、超时 fallback、候选快照/分页。
- [ ] D4 `search_worker` 方向策略、角色权限和跨方向拒绝。
- [ ] D5 kill switch/灰度/legacy fallback（默认关闭时不误写、不泄露）。
- [ ] D6 结果引用包含 `resume_id/listing_ref/listing_version`，可直接用于 Contact。

## E. Action Execution

- [ ] E1 action 创建与 allowlist/profile/policy 版本校验。
- [ ] E2 actor/role/租户权限校验，越权 fail-closed。
- [ ] E3 claim：acquired/replay/busy/terminal 分支及 lease/fencing。
- [ ] E4 finalize：业务事实、结果引用、Session durable commit、ConversationLog、Outbox 同事务。
- [ ] E5 replay：同一 parse artifact/引用重放，不重复 provider/业务写。
- [ ] E6 幂等键 `turn_id + action_name` 与冲突处理。
- [ ] E7 并发冲突/CAS/version 校验整体回滚，不部分成功。
- [ ] E8 审计日志字段完整、脱敏、可按 trace 查询。
- [ ] E9 retryable/terminal 失败重试、dead-letter 和恢复。

## F. Contact / PII

- [ ] F1 ContactRequest 创建、重复请求幂等、actor 校验。
- [ ] F2 Job/Resume `listing_ref`、`listing_version`、`aggregate_version`、`policy_version`、`direction` 全绑定。
- [ ] F3 `authorize -> issue grant -> redeem` 顺序与权限重检。
- [ ] F4 grant 一次性消费：并发仅一个 delivery，replay/跨 actor/跨 listing 拒绝。
- [ ] F5 过期、撤销、版本变化、策略变化立即失效；发送前撤销阻断。
- [ ] F6 PII 加密/密钥版本/脱敏；明文不进 Prompt、索引、Card、日志、Outbox content。
- [ ] F7 ContactDelivery/Outbox 同事务创建，provider 超时复用同 delivery 重试。
- [ ] F8 频控：listing/actor/daily 边界；Redis 不可用 fail-closed 或安全上限。

## G. 岗位发布

- [ ] G1 草稿 DTO/profile 字段白名单、媒体 pending 状态。
- [ ] G2 多轮字段补齐、TTL、重复 operation/source_msg 幂等。
- [ ] G3 `confirm_job` 显式确认/取消，未确认不得发布。
- [ ] G4 确定性审核、敏感词/LLM 建议仅作受控输入、拒绝可解释。
- [ ] G5 激活状态机：passed、activated/expires、可见性与搜索一致。
- [ ] G6 媒体绑定 `entity_version`，替换候选与旧媒体清理。
- [ ] G7 过期/下架/恢复规则及不可恢复状态（deleted/replaced）。
- [ ] G8 候选清理、后台编辑和管理员/角色权限、版本冲突。

## H. 简历发布

- [ ] H1 worker 简历首发草稿/审核/激活全链路。
- [ ] H2 媒体绑定与 `entity_version` 校验。
- [ ] H3 替换、并发冲突、旧简历 replaced/tombstone 与搜索隔离。
- [ ] H4 过期/下架/恢复和候选清理。
- [ ] H5 后台编辑、权限、审核拒绝/编辑事件可审计。
- [ ] H6 版本双写（旧 `version` 与 `aggregate_version`）单调一致。

## I. 一致性与 Outbox

- [ ] I1 `version + aggregate_version` 单调双写，乱序/重复/stale guard。
- [ ] I2 事务内 domain event；失败回滚不留孤立事件。
- [ ] I3 Job/Resume tombstone 防止旧事件复活。
- [ ] I4 consumer scheduler：claim、lease、fencing、checkpoint。
- [ ] I5 retry/dead-letter、可重入恢复和指标。
- [ ] I6 乱序/重复/stale 事件回源校验状态和版本。
- [ ] I7 未知 aggregate/ref fail-closed，禁止创建幽灵事实。

## J. 数据库与运维

- [ ] J1 Phase14 migration up：domain outbox、media version、lifecycle/consumer schema/index。
- [ ] J2 Phase14 migration down：可逆且不破坏既有数据（或有明确非破坏说明）。
- [ ] J3 Phase15 migration up/down：resume outbox、contact direction binding。
- [ ] J4 schema/index/unique/check 约束与 manifest/checksum/preflight。
- [ ] J5 S4/S5 preflight：门禁失败保持 off，报告可审计。
- [ ] J6 kill switch、rollout/rollback、legacy exit gate。
- [ ] J7 启动入口、worker/scheduler、健康检查和配置默认值。
- [ ] J8 MySQL/Redis 探测、连接失败降级策略和恢复。

## K. 兼容和安全

- [ ] K1 legacy API/route/Session/Card 保留并可回放。
- [ ] K2 默认开关 fail-closed；配置缺失/非法值不放行。
- [ ] K3 角色越权、跨方向串线、跨租户/跨 actor 全拒绝。
- [ ] K4 明文 PII 静态/运行时扫描为 0（Prompt/index/Card/log/Outbox）。
- [ ] K5 错误不吞：用户可见安全错误、服务端原因码、重试/死信可追踪。
- [ ] K6 审计可追溯：actor、action、listing/ref、版本、policy、trace、结果。

## 页面级 Golden Flows（必须逐条记录）

对每条 flow 记录：用户身份/角色、逐条输入、逐条可见回复、`listing_ref`、`version/aggregate_version`、状态变化、Contact grant/delivery/outbox、方向和权限断言。

- [ ] GF1 factory 发布岗位 -> worker 搜索 -> 联系 -> ContactDelivery。
- [ ] GF2 broker 发布岗位 -> worker 搜索 -> 联系。
- [ ] GF3 worker 发布/替换简历 -> factory 搜索 -> 联系。
- [ ] GF4 worker 发布/替换简历 -> broker 搜索 -> 联系。
- [ ] GF5 缺字段多轮补充。
- [ ] GF6 confirm 重复/replay。
- [ ] GF7 过期/下架后联系拒绝。
- [ ] GF8 跨 actor/跨方向联系拒绝。
- [ ] GF9 kill switch fallback。
- [ ] GF10 Outbox 重试/dead-letter。

## 命令与证据索引（执行时填写）

| 编号 | 命令/入口 | 环境 | 结果 | 证据路径/摘要 | 影响/建议 |
|---|---|---|---|---|---|
|  |  |  |  |  |  |

## 完成判定

- 清单覆盖率：`已执行通过项 / 总项`，BLOCKED/SKIPPED 必须说明原因。
- 页面级四流：四条均需满足方向、权限、版本、Contact/Outbox 断言；任一关键断言失败不得正式结项。
- WSL/依赖：S4/S5 定向、legacy 核心、全量 unit、compileall、diff check、MySQL/Redis、Phase14/15 up/down 均有证据。
- 正式结项：仅当无未解释 FAIL、关键项无 BLOCKED，且生产 rollout/观察窗口门禁另有明确签字状态。
