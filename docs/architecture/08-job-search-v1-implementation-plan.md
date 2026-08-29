# 08 求职搜索 v1 首版详细实施方案

> 范围：仅实现 worker 通过企微搜索岗位（`worker -> recruitment.job`）。  
> 不在本方案实现：岗位发布、简历发布、factory/broker 找工人、二手物品、远程 MCP、向量库和新的公共事实表。  
> 兼容要求：legacy 搜索始终可用；任何新路径都不能破坏现有 `search_jobs` 行为。

## 1. v1 目标和成功定义

用户可以用自然语言开始找岗位，跨轮次追加/替换/删除城市、工种、薪资等条件，在无结果时接受有限放宽，使用“更多”消费同一候选快照，并从卡片进入受控联系入口。服务端始终按 worker 身份、岗位审核/生命周期和脱敏策略裁决；LLM 只解析语义。

### 1.1 v1 功能闭集

| 动作 | v1 行为 | 不做 |
|---|---|---|
| `start_search` | 建立/切换 `job_search` Session，解析首轮条件并搜索 | 自动跨领域识别、模型自由规划 |
| `modify_search` | 对当前 criteria 做 add/replace/remove，重新生成快照 | 未经确认的隐式放宽 |
| `show_more` | 按快照顺序取下一批，跳过已失效/越权条目 | 重新全量 rerank 或改变原查询 |
| 低召回 | 按固定顺序提出/执行一个放宽步骤，保留原条件 | 任意删除硬条件、二次无确认放宽 |
| 联系入口 | 卡片只返回 opaque `contact_request_id` | 在 v1 回复明文手机号/微信号 |
| fallback | 解析、Facade、rerank、Redis 或新 Runtime 异常时转 legacy | 因新路径失败而丢消息或改变旧结果 |

### 1.2 目标 SLO 与停止条件

目标：普通搜索端到端 P95 ≤5 秒；命令/取消/`show_more` P95 ≤1.5 秒；有效消息持久化 ≥99.99%；Outbox 最终发送 ≥99.9%；Tool 5xx <0.5%；权限/联系方式泄露、重复高风险写操作、非法状态迁移均为 0。

连续 15 分钟或累计 1000 条新 Runtime 请求中出现以下任一项，立即停止扩大灰度并切回 legacy：权限/联系方式泄露、岗位越权或已过期岗位返回、P95 >8 秒、LLM fallback >10%、Tool 5xx >2%、入站最老消息 >120 秒或积压增长、Outbox 失败 >1%/死信 >20 条/小时。回滚不删除搜索 Session、候选快照或已提交 Outbox。

## 2. 当前实现基线与目标差异

### 2.1 当前已存在

- [`backend/app/api/webhook.py`](../../backend/app/api/webhook.py)：验签、解密、Redis 消息去重、限流、`wecom_inbound_event` 写入和入队；当前代码仍在入站落库前做限流通知/丢弃，与目标 durable inbox 顺序有差异。
- [`backend/app/services/worker.py`](../../backend/app/services/worker.py)：消息 claim/lease、同用户串行锁、Session commit、出站 Outbox、重试和恢复循环。
- [`backend/app/services/message_router.py`](../../backend/app/services/message_router.py)：legacy 意图路由以及 Dialogue v2 dual-read/primary 分支；搜索、放宽、`show_more` 和上传冲突分支仍集中在此大文件。
- [`backend/app/services/intent_service.py`](../../backend/app/services/intent_service.py)：显式命令/`show_more` 规则、legacy `IntentResult`、Dialogue v2 解析、灰度和失败 fallback。
- [`backend/app/services/dialogue_reducer.py`](../../backend/app/services/dialogue_reducer.py)、`dialogue_applier.py`：已有 `resolve_conflict`、`respond_relaxation_offer`、criteria merge、awaiting 和 Session 状态物化。
- [`backend/app/services/conversation_service.py`](../../backend/app/services/conversation_service.py)、[`backend/app/schemas/conversation.py`](../../backend/app/schemas/conversation.py)：已有 `SessionState`、`candidate_snapshot`、`shown_items`、`last_criteria`、`pending_relaxation`、search awaiting TTL/CAS 字段。
- [`backend/app/services/search_service.py`](../../backend/app/services/search_service.py)：已有 worker->job SQL 硬过滤、Top-N/LLM rerank、候选快照、零结果放宽、推荐实验、脱敏投影和 legacy fallback。
- [`backend/app/services/search_permission.py`](../../backend/app/services/search_permission.py) 与 visibility policy：已有角色和可见性决策入口。
- [`backend/app/models.py`](../../backend/app/models.py)：`Job` 具有 `audit_status`、`expires_at`、`deleted_at`、`version` 等字段；`WecomInboundEvent` status 闭集为 `received/processing/session_pending/done/failed/dead_letter`；已存在推荐请求/尝试/投递表。

### 2.2 v1 尚未具备

以下是本方案的目标改动，不应在发布前标记为“已有”：

- 内部版本化 `DialogueParseResult`（补 `schema_version=dialogue.v1` 的兼容 wrapper）；
- worker->job 专用 Listing Search Facade 和结构化 `ListingCard`；
- 统一的 `turn_id + action_name` Action lease/fencing（搜索读动作也记录执行事实，后续写动作可复用）；
- `jobs.aggregate_version`/`domain_outbox_event` 的完整一致性链路（本 v1 只做读取兼容和预留，不新增公共 listing 表）；
- 独立 Contact Domain Service 的 token/频控/撤销/PII 闭环（本方案只接入 opaque 联系入口，明文兑换以 S3 为前置条件）。

## 3. 分阶段实施

### Phase 0：基线、可靠入站与回放集

#### 功能需求

1. 固化至少 100 个 worker 找岗位 golden cases：完整首句、补城市/工种/薪资、替换和删除条件、无结果、放宽接受/拒绝、`show_more`、取消/reset、过期岗位、越权/封禁、LLM 超时/非法 JSON、Redis/企微出站失败。
2. 对同一输入记录 legacy intent、v2 parse/decision、criteria、候选 ID/顺序、回复摘要和 fallback 原因，原文按现有 TTL 与 PII 策略脱敏。
3. 有效消息在限流判定前写入 `wecom_inbound_event`。限流消息使用 `status=done` + `rate_limit_decision=rate_limited`，正常消息保持 `status=received` + `accepted`。
4. 入队失败可由 dispatcher 扫描 `status=received AND rate_limit_decision=accepted` 恢复；数据库不可用时返回非 2xx 让企微重试。

#### 当前代码现状

`webhook.py` 当前文档和实现仍显示“限流消息不写入 inbound_event”；`WecomInboundEvent` 没有目标 `turn_id`、限流决策列。`worker.py` 已有启动恢复和 lease，但不能依赖 Redis L1 作为唯一持久化事实。

#### 目标行为

每个有效消息都有不可变 `turn_id`，重复投递命中数据库唯一键后复用原事件；限流有审计记录且旧 Worker 能按闭集 status 忽略；golden replay 可以在不调用真实企微/Provider 的情况下重放并比较。

#### 具体代码改动范围

- `backend/app/api/webhook.py`：调整接受顺序；抽取 `_insert_inbound_event` 参数以写 `turn_id`/限流字段；限流通知只入非关键队列。
- `backend/app/models.py` 与 Alembic/SQL migration：增加兼容列和索引，绝不新增 `accepted` status。
- `backend/app/services/worker.py`：增加 accepted 事件 dispatcher、恢复指标和 turn_id 透传。
- `backend/app/services/intent_service.py`/`message_router.py`：补 trace/replay hooks，不改变 legacy 路由结果。
- `backend/tests/`：新增 webhook 顺序、Redis/DB 故障和 replay contract tests。

#### 数据库/配置改动

增加 `wecom_inbound_event.turn_id`（唯一）、`rate_limit_decision`（`accepted/rate_limited`）、`rate_limit_rule`、`rate_limited_at`；已有行按 `msg_id` 回填 turn_id，无法回填的进入人工核查。配置沿用 `rate_limit.window_seconds/max_count`，新增 dispatcher 扫描批量、lease 和 replay 样本率配置。

#### 新旧兼容

旧 Worker 读取原 status 不受影响；新字段缺失按 `accepted` 仅适用于迁移前历史行，并写兼容指标。`msg_id` 仍是企微去重键，人工重放创建新 turn，不能复用已完成 turn 的 Action 幂等键。

#### 测试用例

- 签名有效、DB 成功、Redis 入队失败：返回 200，事件保持 `received` 并可补投。
- 限流消息：事件为 `done/rate_limited`，不进入业务 Worker，不重复推送限流提示。
- DB 不可用：返回 503，企微重试后只产生一条事件。
- Redis L1 stale dedup marker：数据库唯一键仍可接受消息。
- 100 个 golden case 的 legacy 输出摘要和候选顺序无变化。

#### 验收、灰度与回滚

验收为有效消息持久化 ≥99.99%、丢失 0、最老未处理 <60 秒、回放差异均有登记。先 staging 全量和生产 shadow；若出现消息丢失/重复或 backlog 停止条件，关闭新接入开关，保留新增列和审计数据，不回滚已落库事件。

### Phase 1：Dialogue v1、Session 与 Reducer 适配

#### 功能需求

- 固化 `DialogueParseResult` v1 闭集及 `conflict_action`/`relaxation_response` 互斥校验。
- `IntentResult` 只能通过显式映射进入 v1；Provider 缺 `schema_version` 时由 adapter 补值，未知 schema/act/slot 走规则/legacy fallback。
- Reducer 只生成确定性 decision：criteria patch、awaiting、快照失效、放宽确认、状态转换；LLM 不能写 Session 或数据库。
- Session 记录 `schema_version`、`profile=recruitment.job`、`session_version`，并维护 legacy compatibility projection。

#### 当前代码现状

`llm/base.py` 的 `DialogueParseResult` 已有 act 闭集和冲突/放宽校验，但没有 `schema_version`；`intent_service.py` 已实现 v2 `off/shadow/dual_read/primary`；`dialogue_reducer/applier` 与 router 已有对应分支。当前是双写/双读并存，不是纯 v1 Runtime。

#### 目标行为

worker 找岗位请求在命中灰度桶时走 `DialogueRuntime -> Reducer -> Search Facade`，未命中或解析失败自动走原 `IntentResult -> message_router -> search_service.search_jobs`。`resolve_conflict` 和 `respond_relaxation_offer` 的兼容行为必须与现状一致。

#### 具体代码改动范围

- `backend/app/llm/base.py`：保持旧 DTO 字段，新增内部 `VersionedDialogueParse` wrapper/校验，不强制旧 Provider 立即返回 schema_version。
- `backend/app/services/intent_service.py`：集中 v1 adapter、版本/未知 slot 记录和 fallback reason。
- `backend/app/services/dialogue_reducer.py`、`dialogue_applier.py`：只补 worker->job 所需 profile guard、criteria merge 和 decision trace。
- `backend/app/services/conversation_service.py`、`schemas/conversation.py`：增加 schema/profile 字段的向后兼容读写和 projection helper。
- 新增 `backend/app/conversation/runtime.py`（或同等 adapter）：封装 parse/reduce/route，禁止在 `message_router.py` 增加新的业务分支。

#### 数据库/配置改动

Session 存储增加 `schema_version/profile`（Redis JSON 兼容默认值）；对话日志/trace 增加 parse schema、runtime mode、fallback reason 字段或 JSON 扩展。配置复用 `dialogue_v2_mode` 兼容别名、白名单、hash bucket、primary rollout，不新增无法回退的硬开关。

#### 新旧兼容

旧 Session 缺字段按 `dialogue.v1`/`recruitment.job` 推断但不覆盖原值；legacy 读取新 Session 依赖 projection。v2 只在 worker->job 目标桶生效，broker/factory 和上传流程保持原路径。

#### 测试用例

- 每个 v1 act、非法枚举、未知 slot、低置信度和空 Session 的 schema/reducer 单测。
- 条件 `城市+工种` 首轮、下一轮薪资替换、列表追加/删除、`resolve_conflict`、放宽 accept/reject、过期 awaiting 回放。
- 同一输入 legacy 与 v1 的 criteria/候选顺序/回复模板 diff；差异必须在允许清单内。
- Session version CAS 冲突时只保留一个提交者，另一个 fallback/retry。

#### 验收、灰度与回滚

协议/Reducer 契约测试 100% 通过；非法 Flow 迁移 0；v1 fallback 率基线可解释。先 shadow，再 1% worker 白名单，随后 5%/25%/50%；任一全局停止条件或 v1/legacy 差异扩大即把 `dialogue_v2_mode=off`，保留新 trace，不迁移回旧 Session 内容。

### Phase 2：Search Facade 与 ListingCard

#### 功能需求

提供单一 `search_jobs_v1(actor, criteria, session, turn)` 入口：

1. 校验 `ActorContext.role=worker` 和 `recruitment.job` profile。
2. 将 criteria 映射到现有 SQL 硬过滤：城市、工种、薪资、班次/长期短期等已支持字段。
3. 调用现有 `search_service.search_jobs` 的候选查询和 rerank；控制 Top-N、候选上限和超时预算。
4. 读取前再次检查 `audit_status=passed`、`deleted_at IS NULL`、未过期/下架、用户 active 和可见性策略。
5. 输出结构化 `ListingCard`：opaque `listing_ref`/`contact_request_id`、标题、摘要、城市、薪资/班次等允许属性、解释和失效提示；不输出 phone/微信号。
6. 将候选顺序、criteria digest、算法/策略版本保存到 Session snapshot 和推荐事实表（沿用现有表）。

#### 当前代码现状

`search_service.py` 已同时承载 SQL、rerank、推荐实验、fallback、快照和文本渲染，返回类型包含 `SearchResult/SearchOutcome`；没有稳定的跨领域 `ListingCard` Facade。现有 worker->job 搜索行为是必须保持的 legacy baseline。

#### 目标行为

Router 只调用 Facade；Facade 通过 adapter 调旧 Service，而不是复制 SQL。Facade 失败、超时或结果 schema 不合法时返回 legacy 结果并记录 `facade_fallback`。企微文本由固定 renderer 从 Card 生成，Card 可供未来后台/小程序复用。

#### 具体代码改动范围

- 新增 `backend/app/listing/search.py`：定义 `JobSearchFacade`、输入/输出 schema、legacy adapter 和错误码。
- 新增 `backend/app/listing/render.py`：定义 `ListingCard` 到企微文本的稳定模板和脱敏边界。
- `backend/app/services/search_service.py`：只增加 Facade 所需的投影/查询 adapter 和 telemetry，不改 legacy 分支排序。
- `backend/app/services/message_router.py`：在 worker->job 目标分支调用 Facade，保留 `_handle_search` legacy fallback。
- `backend/app/schemas/search.py`：补 Card/criteria digest/invalid item schema。
- `backend/tests/`：Facade contract、Card 字段白名单、legacy equivalence tests。

#### 数据库/配置改动

不新增 `listing` 事实表，不改 jobs/resumes 双写。可增加推荐请求/投递 JSON 的 `profile`, `listing_ref`, `contact_request_id`, `facade_version`；如使用索引，索引是派生数据，查询仍回源校验。配置新增 `JOB_SEARCH_FACADE_ENABLED`、`JOB_SEARCH_FACADE_ROLLOUT_PERCENTAGE`、`JOB_SEARCH_FACADE_TIMEOUT_MS`，默认关闭/0%。

#### 新旧兼容

`listing_ref` 使用 `recruitment.job:{job.id}`，legacy 文本中原有岗位编号保持不变，Card 只在新 Runtime 输出。旧 Session 的数字 candidate IDs 由 adapter 转为 ref；无法转换时直接 legacy。旧推荐策略 kill switch 优先级高于 Facade 开关。

#### 测试用例

- 同一 criteria 下 Facade 与 legacy 的候选集合、顺序、空结果和放宽建议等价。
- 任意 Card 序列化不含 phone/微信号/内部 SQL 字段。
- jobs 处于 pending/rejected/expired/delisted/deleted、owner inactive、worker 无权限时均不返回。
- rerank timeout/http error/parse failure 时硬过滤结果和固定模板可用。
- `show_more` 使用 snapshot，不触发第二次全量 rerank。

#### 验收、灰度与回滚

staging shadow compare 通过率 ≥99%（差异仅限已批准的排序/文案字段）；普通搜索 P95 ≤5 秒；权限泄露 0。生产按 worker hash 1% 起步；停止时关闭 Facade 开关，Router 直接走原 `search_jobs`，无需数据回滚。

### Phase 3：多轮条件修改、放宽与 show_more

#### 功能需求

- `criteria` 使用显式 patch：`add/update/remove`；列表字段的 `replace/add/remove/unknown` 由 Reducer 结合上下文裁决。
- 每次有效修改递增 `ranking_version`、失效旧 snapshot、保存 `last_criteria` 和 query digest。
- `show_more` 只消费未过期 snapshot，按原顺序跳过已下架、过期、越权和已展示条目；快照耗尽给固定文案。
- 低召回按固定顺序（例如薪资 10% -> 工种大类 -> 去可选条件）逐步建议；一次确认只执行一步，二次确认使用 `pending_relaxation.original_criteria` 和原始 query。
- 解析确认回复兼容 `respond_relaxation_offer`；上传冲突兼容 `resolve_conflict`，但 worker->job v1 不新增上传行为。

#### 当前代码现状

`SessionState` 已有 `candidate_snapshot`、`shown_items`、`pending_relaxation`、`awaiting_fields`；`search_service.show_more` 已复用快照并有 kill switch；`message_router.py` 已有放宽确认短路和原始 query 复用逻辑。目标是把这些行为收敛到 Facade/Reducer 契约，不重写成熟逻辑。

#### 目标行为

任意一轮修改都有可追踪 patch 和 before/after criteria；用户说“更多/再来几条”不会改变条件；用户说“可以放宽”只执行已展示的那一步；快照过期或策略 kill switch 时明确提示并要求重新搜索，不能返回陈旧岗位。

#### 具体代码改动范围

- `dialogue_reducer.py`/`dialogue_applier.py`：补 job_search profile 的 patch 白名单、快照失效和放宽 transition contract。
- `conversation_service.py`：封装 snapshot create/consume/expire，保持现有 Redis key/TTL 兼容。
- `listing/search.py`：实现 `modify_search`、`show_more`、`relax_search` Facade action。
- `search_service.py`：复用现有 `show_more`、`execute_relaxed_search`；仅增加 Card 投影和 telemetry。
- `message_router.py`：把现有分支接到 Facade adapter，删除计划中的新分支不得超过一个路由点。

#### 数据库/配置改动

沿用 Redis Session TTL（当前搜索 awaiting 默认 600 秒、候选快照 30 分钟语义）并由配置集中管理；推荐请求/尝试记录 `attempt_kind=initial/relax_probe`、criteria digest、snapshot_id、algorithm_version。若增列，采用 nullable/默认值，旧 Worker 可忽略。

#### 新旧兼容

旧快照没有 `snapshot_id/direction` 时由 adapter 按 `search_job` 推断；旧 `broker_direction` 非 `search_job` 不进入本 Facade。放宽字段缺失或过期按 legacy 处理，不猜测用户接受。

#### 测试用例

- 首轮 `深圳 普工 6000`，下一轮“成都也可以”“薪资改 7000”“不要夜班”；检查 patch 和 SQL 条件。
- 0 命中后接受薪资放宽，再次发送“好的”不得二次放宽；拒绝后原条件保持不变。
- snapshot 过期、岗位在两轮间下架、用户被封禁、候选重复时 `show_more` 均跳过并可解释。
- 并发两次 `show_more`：Action lease/CAS 防止同一批重复消费。

#### 验收、灰度与回滚

回放中条件 merge 正确率 ≥99%，`show_more` 重复展示率为 0，放宽未确认执行率为 0。灰度期间可单独关闭 `JOB_SEARCH_V1_RELAXATION_ENABLED` 或 Facade；关闭后保留 legacy `show_more` 行为和旧快照读取。

### Phase 4：权限、过期、脱敏与联系入口

#### 功能需求

- 所有搜索/翻页/放宽 action 重新生成服务端 `ActorContext`，不信任 LLM/Session 中的 role/userid。
- 结果查询必须检查 worker 账号 active、黑名单、岗位 `audit_status=passed`、`expires_at`、`deleted_at/delist` 和 visibility policy。
- Card 只展示批准字段；联系人姓名可按策略展示，phone/微信号只在 Contact Domain Service 中受保护存储。
- Card 的联系入口携带不可猜测 `contact_request_id`，v1 可只发送“点击/回复联系”引导；明文兑换、短 token、频控和撤销由 Contact Service/S3 完成。
- 联系点击/拒绝/过期均写审计和 trace；不把原始 PII 写入 `ConversationLog.content`、Prompt、索引或 Outbox payload。

#### 当前代码现状

`search_permission.py`、visibility policy 和 `search_service.py` 已做部分角色/审核/active 过滤；`Job`/`User` 仍存在联系人/phone 字段；尚无完整独立 Contact Domain Service、opaque token 和撤销模型。

#### 目标行为

搜索结果即使来自旧索引/快照，也在返回前回源复核；岗位过期或下架立即不可见。没有 Contact Service 时绝不降级为明文展示，而是返回受控的“联系入口暂不可用/请通过平台联系”文案。

#### 具体代码改动范围

- `listing/search.py`/`render.py`：字段白名单、Card contact action 和回源 visibility check。
- `search_permission.py`/`visibility_policy.py`：统一 worker->job policy decision 和拒绝码。
- 新增 `listing/contact.py`（本阶段只定义 request 创建/审计接口；token 兑换若未完成则 feature-off）。
- `models.py`/迁移：按 S3 设计新增 contact request/token/audit 表前先评审，不能把 phone 放入搜索表。
- `message_router.py`/`worker.py`：联系入口回复走 Outbox，失败可重试。

#### 数据库/配置改动

新增配置：contact feature kill switch、token TTL（建议 60 秒）、actor+listing 10 分钟 3 次、actor 每日 30 次、审计保留期。phone 加密迁移、密钥轮换和旧明文清理不在搜索 Facade 内完成，需 S3 单独迁移任务。

#### 新旧兼容

legacy 搜索继续使用其现有脱敏模板；Facade 开关关闭时不改变旧回复。旧快照中的联系人字段一律忽略并重新查询；发现快照含 PII 立即使其失效并告警。

#### 测试用例

- worker、factory、broker、inactive/blacklisted actor 的搜索和联系权限矩阵。
- pending/rejected/expired/delisted/deleted 岗位在首轮、`show_more` 和放宽后均不可见。
- Card、trace、outbox、LLM prompt 快照做 PII 扫描，手机号/微信号命中为失败。
- contact_request 不可猜 ID；token 重放、过期、下架、封禁、策略变更后均拒绝并审计。

#### 验收、灰度与回滚

权限/PII 泄露为 0；token 一次性、频控和撤销符合配置；审计可按 trace/actor/listing 查询。联系入口独立按 0% -> 白名单灰度；任意泄露立即关闭 contact switch，搜索结果仍可返回脱敏卡片并回到 legacy。

### Phase 5：端到端灰度、监控和运营回滚

#### 功能需求

- 建立 worker hash/白名单灰度，配置变更同时作用于 app 与 worker；记录 runtime/profile/skill/schema/facade/algorithm 版本。
- Dashboard/告警覆盖：搜索请求量、无结果率、放宽接受率、Card 渲染失败、legacy fallback、P50/P95/P99、LLM token/超时、Tool/DB/Redis 错误、snapshot 过期、权限拒绝、contact 拒绝、inbound backlog、Outbox 失败/死信。
- 每次回滚记录开关、操作者、原因、受影响用户/turn 和恢复时间；已提交业务/Outbox 不撤销。

#### 当前代码现状

推荐实验已有 `recommendation_runtime_control`、strategy assignment、shadow budget 和 kill switch；Dialogue v2 已有 whitelist/hash bucket/primary rollout；Worker 已有 Outbox 和恢复循环，但缺少本方案 Facade 级统一看板与停止条件。

#### 目标行为

运营可以独立关闭 Dialogue v1、Search Facade、放宽、Contact 和推荐体验；关闭某一开关不会清理 Session 或改变事实源。故障时新消息在一个 turn 内稳定转 legacy，不出现半新半旧的重复回复。

#### 具体代码改动范围

- `config.py`/`api/admin/config.py`：增加/映射 v1 开关、百分比、预算和超时，保留旧配置别名。
- `tasks/worker_monitor.py`、日志/metrics：补 SLO 聚合和停止条件告警。
- `worker.py`/`message_router.py`：统一 fallback reason、回滚事件和未完成 turn 恢复。
- `backend/tests/`：灰度矩阵、kill switch、故障注入和回滚演练。

#### 数据库/配置改动

只增加观测 JSON/事件表字段或复用已有推荐/审计表；禁止把灰度状态放在不可回滚的 Session 业务字段。配置默认：Facade/Contact/Dialogue v1 rollout=0，legacy 为主。

#### 新旧兼容

回滚路径必须在旧 Provider、旧 Session、旧快照和旧文本模板都可用的环境中演练；保留至少一个版本的旧 prompt/parser 和旧搜索配置，直到 v1 稳定满一个完整观察窗口。

#### 测试用例

- 逐项关闭开关：v1 parse 失败、Facade timeout、rerank 失败、Redis snapshot 不可用、Contact Service 5xx、Outbox 失败。
- 15 分钟/1000 请求停止条件自动触发并把流量切回 legacy。
- Worker 崩溃、lease 过期、重复消息、Session CAS 冲突和发送重试的端到端演练。
- staging 及生产小流量对比成本、P95、候选集合和用户回复。

#### 验收、灰度与回滚

连续观察窗口满足 SLO、无安全事故、fallback 和差异均低于门槛后才可从 1% 提升至 5%/25%/50%/100%。任何停止条件触发即冻结扩大，关闭 v1 开关并生成复盘；legacy 搜索行为不得被清理，直到产品/架构负责人签字下线。

## 4. 数据与事务契约（v1 必须遵守）

### 4.1 招聘事实源

v1 只读 `jobs` 及关联用户/字典表，不创建公共 `listing` 表，不对 jobs 做业务双写。索引/推荐快照均为派生数据；返回前回源检查状态、权限和有效期。

### 4.2 Action 幂等与 lease/fencing

每个 inbound event 生成 `turn_id`；搜索动作使用 `action_name=listing.search`、`listing.show_more`、`listing.relax_search` 等稳定名称，幂等键为 `turn_id + action_name`。`action_execution` 的 `started` lease 过期才可抢占并递增 fencing token；只有当前 owner/token 能在同一事务提交 Session CAS、业务事实和成功标记。搜索读动作若已成功，重试直接复用保存的结果 digest/快照，不重复 rerank 或发送重复 Outbox。

### 4.3 Outbox 与消息顺序

入站事件持久化、Session mutation、推荐事实和 Outbox 记录在数据库事务中提交；企微网络调用只在提交后由 Worker 执行。出站失败重试不回滚已生成快照；若用户已收到旧回复，重试仍使用独立 delivery 幂等键并记录可能重复的风险。

### 4.4 未来 `aggregate_version` 预留

本 v1 不改变 jobs 事实表语义，但实现 Facade 时不应阻塞后续 `aggregate_version + domain_outbox_event`。任何新增索引消费代码必须接受版本字段和 tombstone；未完成事件链路前不得宣称“索引最终一致”或切断旧 SQL fallback。

## 5. 交付物与责任边界

交付物包括：v1 协议/Reducer 契约、JobSearchFacade 和 ListingCard schema、回放集/差异报告、数据库兼容 migration、配置与告警、灰度 runbook、故障注入报告、权限/PII 测试报告。产品确认卡片字段与放宽文案；后端负责 Facade/事务/权限；AI 负责 prompt/schema/golden cases；测试负责回放和停止条件；运维负责双进程配置、监控和回滚演练；安全负责 Contact/PII 和密钥方案。

本方案完成不等于岗位发布、简历发布、双向招聘或二手领域完成；这些必须遵循 [07 总体改造路线图](07-overall-migration-roadmap.md) 的后续阶段和独立验收。

