# v1 完成后的 Action Execution 与 Contact 实施方案

> 基线：2026-08-30，Job Search v1 与本方案规划的 S3 Action/Contact 工程实现、代码审查、WSL 部署和 mock 端到端验证均已完成。本文保留原实施设计，同时记录已落地范围和剩余生产门禁。
>
> 重要状态声明：ActionGateway、parse artifact、claim/finalize、结果引用和 replay 已接入 Worker 搜索调用链；Contact Service、一次性 grant/delivery、PII 加密回填、Contact Outbox 和故障演练也已落地。当前生产配置仍为 `action_execution_mode=off`、Contact `off`、`served_assignment=legacy` 优先；工程接入完成不等于生产 on 灰度或 legacy 退出完成。

## 0. 实施进度（2026-08-30）

| 工作流 | 当前进度 | 验证证据 | 尚未完成 |
|---|---|---|---|
| A：Action Execution | A0-A4 工程实现完成；Worker 已在 Router 前完成单次 parse、claim，成功后 finalize，重复 turn 走结果引用 replay | S3 核心回归通过；Action/Worker 定向集合通过；Action preflight 通过 | 生产 `on` 灰度、7 天观察窗口、legacy 退出审批 |
| B：Contact/PII | B0-B4 工程实现完成；搜索卡片真实创建 ContactRequest；显式“联系”入口完成 authorize -> issue grant -> redeem；ContactDelivery/Outbox 按类型投递 | PII verify `ready_for_freeze=true`；Contact/权限/可见性/观测集合通过；WSL contact-on 对话 smoke 通过；无明文 PII 进入 Prompt、Card、Log、Outbox | Contact 生产 on 灰度、旧明文列清理审批、长期 SLO 观察 |
| C：灰度/恢复 | C1/C4 观测与回滚脚本完成，C2 故障矩阵自动化完成；legacy fallback 保留 | C2 故障矩阵 `9/9 passed`；WSL 常规对话 smoke `13/13` 通过；Contact-on 模拟链路通过 | 连续 14 天 Action/Contact on 指标和共同签字 |

本轮联合回归：S2/S3 核心集合 `128 passed`。全量 unit `2426 passed`；剩余 8 例属于既有 Phase 11 manifest checksum、Phase 11 resume visibility 和 Phase 3 job visibility 基线失败，不影响本次 S2/S3 核心验收结论，但在全仓绿灯前仍需另立基线修复任务。

第 3-5 节各小节中的“当前代码现状”保留为方案编写时的实施前基线，用于追踪设计差异；实时完成度以本节和各节状态说明为准。

## 1. 下一步工作排序与 S4 门槛

### 1.1 剩余工作顺序

1. **Workstream A：Action Execution 生产 on 灰度与观察**。工程接入、结果引用和 replay 已完成，剩余是按用户桶逐步开启并观察重复调用、引用完整率和 `session_pending`。
2. **Workstream B：Contact 生产 on、密钥治理与旧列清理**。Contact/PII 闭环已完成，剩余是小流量 on、密钥轮换演练、旧明文列清理审批和长期 SLO 观察。
3. **Workstream C：legacy 退出门槛**。C1/C2/C4 已完成，剩余是连续生产观察窗口、golden replay 差异审批和 legacy 退出签字。
4. **Workstream D（仅在门禁满足后）：岗位发布 S4 预备评审**。S4 依赖 A/B 的生产门禁和 C 的稳定观察窗口，不能并行提前启动。

### 1.2 为什么不能直接进入岗位发布

- 岗位发布是写入事实源、审核状态和附件的高风险流程；虽然 Action 结果引用、replay 和两阶段恢复边界已实现，但生产 on 灰度和完整观察窗口尚未完成，不能把本地验证直接外推为生产稳定性。
- Contact/PII 工程闭环已完成，但 Contact 仍默认 feature-off；旧明文列清理审批、密钥轮换演练和生产 SLO 窗口尚未完成，不能在 S4 中扩大敏感数据写入面。
- C3 的 legacy 退出门槛要求连续 14 天指标、golden replay 差异审批和紧急回退演练。门禁完成前，S4 必须保持独立评审，不得提前启动。

### 1.3 S4 允许启动的必要条件

S4 只有在以下条件全部满足，并由后端、运维、安全/合规和产品共同签字后才能启动：

- A4 工程条件已完成：search/show_more/relaxation 均在调用链使用 claim/fencing；成功 replay 只引用已落库结果、快照和 Outbox，不重新 rerank，不重复创建 delivery。生产 on 观察窗口待完成。
- B4 工程条件已完成：Contact Service、PII 加密回填和抽样校验已通过；phone/微信号不进入 Prompt、索引、普通日志、Card 或 Outbox。Contact 小流量 on、旧明文列清理审批待完成。
- C1-C2 工程条件已完成，C2 故障矩阵 `9/9` 通过；C3 要求的连续生产观察窗口和 legacy 退出签字待完成。
- 旧链路仍可回退：`job_search_facade_enabled=false`、Action/Contact kill switch 和配置回滚均已演练，且回退不删除已提交事实。

### 1.4 明确不在本方案中

不实现岗位发布代码、简历发布/替换、二手物品流程、交易/支付、跨天工作流、远程 MCP、向量库/新搜索引擎、统一招聘事实表迁移，也不重写 `message_router.py`。这些仍按 [07 总体路线图](07-overall-migration-roadmap.md) 的后续 S4-S7 另立方案。

## 2. 现状基线与共同约束

### 2.1 真实代码基线

- `backend/app/services/action_execution_service.py` 提供 `claim_action_execution`、`finalize_action_execution`、`read_action_execution`；幂等键为 `turn_id + action_name`，当前只保存 `request_digest/result_digest`。
- `backend/app/listing/search.py` 的 `JobSearchFacade` 负责 feature gate、legacy adapter、Card 投影和 opaque `contact_request_id`；`show_more` 消费现有 Session snapshot，`relax_search` 调用 `search_service.execute_relaxed_search`。
- `backend/app/services/search_service.py` 已执行硬过滤、rerank、快照、放宽和推荐实验；结果带 `request_id/snapshot_id`。
- `backend/app/services/message_router.py` 的 `_run_search`、`_handle_show_more`、`_route_v2_relaxation_response` 仍直接调用 Facade/legacy service，没有 Action claim/finalize。
- `backend/app/services/worker.py` 的 `_process_locked` 先标记 inbound `processing` 并提交；路由后在同一 DB 事务写 ConversationLog、推荐事实、Outbox、durable Session commit，DB commit 后再做 Redis Session CAS；失败进入 `session_pending` 恢复。
- `backend/app/services/recommendation_delivery_service.py` 已有 `RecommendationRequest`、`RecommendationSearchAttempt`、`RecommendationDelivery` 的事实与快照引用，`prepare_delivery`/`persist_request_fact_only` 可在 Worker 事务中写入。
- `backend/app/models.py` 中 `Job.phone`、`User.phone`、联系人字段仍存在；`backend/sql/migrations/phase10_001_job_visibility_fields.sql` 增加了岗位联系人字段。当前隐私测试主要验证投影不泄露，不等于 PII 加密迁移已完成。

### 2.2 不变约束

1. `turn_id + action_name` 是唯一幂等键；lease 过期才可抢占，fencing token 单调递增。
2. Action 成功只能与业务事实、推荐 request/attempt、Outbox 和 durable Session commit payload 一起提交；Redis CAS 仍是 DB commit 后的第二阶段，不能伪装成同一 ACID 事务。
3. replay 先读取成功 Action 的结果引用，补投现有 Outbox 或恢复 `session_pending`，绝不重跑 provider/rerank/放宽 probe。
4. 所有新路径都有 `off/shadow/on` 或按用户桶灰度，legacy fallback 保留到 C4 签字退出；kill switch 只切路由，不删除已提交数据。
5. Contact 明文只在服务端授权的短暂内存范围出现；不得写入 Prompt、索引、普通日志、`ListingCard`、`RecommendationRequest`、`RecommendationDelivery` 或 Outbox content。

## 3. Workstream A：Action Execution 生产接入

> 实施状态：A0-A3 已完成并接入 Worker；A4 的 preflight、kill switch、replay 观测和门禁测试已完成。生产默认仍为 `off`，因此当前日志中 legacy 流量仍是预期行为。

### A0：结果引用与 replay 契约（先于任何生产 claim）

**功能需求**

- 为每个 Action 保存可定位的 `result_ref`：`request_id`、最终 `snapshot_id`、delivery/outbox 主键集合、Session commit 记录和结果 schema/算法版本；结果正文不直接塞入 Action 行。
- 明确 action 类型：`search_job`、`show_more_job`、`relax_job`，后续可扩展但不允许自由字符串分支。
- 成功 replay 必须验证引用完整性和 actor/turn 绑定；缺引用进入 terminal/人工修复，不重新执行。

**实施前基线（已完成替换）**

`ActionExecution` 只有 `result_digest`；推荐事实已经有 `request_id/snapshot_id`，但 Action 与它们没有外键或关联。`worker.py` 的 Session CAS 晚于 DB commit（见 09 审计）。当前 `message_router.process` 内部还会调用 `classify_intent/classify_dialogue`，所以若 Gateway 另行解析而不传递 parse，会形成一次 turn 两次解析。

**目标行为**

一次成功 Action 的 DB 事务原子写入：Action finalize、`RecommendationRequest`/`RecommendationSearchAttempt`、`RecommendationDelivery`/`WecomOutboundOutbox`、ConversationLog、durable Session commit。提交后 Redis CAS 失败只进入 `session_pending`，replay 只执行 CAS 和 Outbox 投递。

**预路由与 claim 边界（必须先实现）**

当前 Worker 在调用 `message_router.process` 前并不知道本轮是搜索、翻页还是放宽，因此不能把 claim 写成“Router 前随手调用”。A0 先新增一个无副作用的 `ActionGateway`（建议文件 `backend/app/services/action_gateway.py`），由 Worker 在进入 Router 前调用：

1. Gateway 读取已解密的 inbound message、用户角色和只读 Session hint，按显式命令（`更多`、放宽确认/拒绝）、pending relaxation、`search_job` 角色/profile 和轻量 slot/intent parser 生成 `ActionEnvelope`。必要时可复用 `intent_service` 的严格超时 classify，但不得执行搜索、写 Session、调用 provider 或写联系方式。
2. Envelope 固定包含 `turn_id`、`action_name`（`search_job`/`show_more_job`/`relax_job`/`none`/`unknown`）、`request_digest`、`classifier_version`、`legacy_reason` 和 trace；无法确定时返回 `unknown`，不能猜测为写入型 Action。
3. 仅当 `action_execution_mode=on` 且 envelope 是三个受支持 action、用户在 rollout 桶内时，Worker 才在独立短事务调用 `claim_action_execution` 并提交 claim，然后把带 owner/fencing token 的 envelope context 传给 `message_router.process`。Router/Facade 不再 claim，只校验 context 与自身最终 action 一致。
4. `off` 或不在 rollout 桶：Gateway 不解析、不创建/不 claim Action，直接走现有 legacy Router（Router 自己进行且只进行一次 legacy parse），并记录 `legacy_reason`。在 `on/shadow` 中，`none`/`unknown` 已经是 Gateway 单次 parse 的结果：不 claim，但必须把同一 `parse_ref` 传给 Router，走 `legacy_from_parse` 或确定性的 fallback；不得让 Router 再次 classify，也不得在搜索执行完后补 claim。
5. claim 返回 `acquired`：执行 Router；返回 `succeeded`：绕过 Router，走 replay reference；返回 `in_progress`：不执行 Router，事件退避重试/等待 lease；返回 `failed_terminal`：不执行 Router，返回已持久化的 terminal 结果或进入人工队列；request digest 冲突是安全错误，立即停止本轮。

预路由只负责“识别候选 action”，不负责权限裁决。权限、条件校验和最终 action 仍由 Router/服务端执行；若 Router 发现 envelope 与实际 action 不一致，必须 rollback 本轮并将 Action 标为 `failed_terminal(action_mismatch)`，不得执行第二个 action。

**解析复用、`parse_ref` 与语义一致性（固定采用单次解析方案）**

- `ActionGateway` 不实现第二套意图语义。`action_execution_mode=on` 或 `shadow` 时，它调用新增的 `intent_service.classify_for_action_gateway` 适配器；该适配器沿用现有命令优先级和 schema，但对 provider/extractor 只调用一次，在同一次调用中产出 `DialogueParseResult`、兼容 `IntentResult` 和 `ActionEnvelope`。解析错误在适配器内转为 `unknown/defer`，不得再调用 legacy classifier；Gateway 之后不得再次调用 LLM 或 `classify_intent`。
- Envelope 携带 `parse_ref`（UUID）、`parse_digest`、`parse_schema_version`、`classifier_version`、`session_version`、`parse_expires_at` 和 PII-free 的结构化 parse。Router 的 `process(msg, db, action_context=...)` 必须直接消费这份 parse；`action_context` 存在时，`_handle_text` 跳过 `classify_intent/classify_dialogue`，只执行确定性的 reducer/权限/业务路由。
- `parse_digest = SHA256(canonical(parse_payload) + session_hint_digest + classifier_version + schema_version)`；Action 的 `request_digest` 还必须绑定 `parse_digest`、action name 和 criteria digest。Router 重新计算 digest，不匹配即视为 stale/bug，不得把另一份 parse 当作替代。
- 新增 `ActionParseArtifact`（或等价持久化表）保存 allowlist 后的 parse payload；不保存原始消息、Prompt 或 phone/微信号。`parse_ref` 在 Redis 热缓存中保留 60 秒，在 DB artifact 中保留 24 小时或直到 Action terminal，以覆盖 lease/retry。缓存 miss 时优先读 DB artifact；已 claim 的 turn 不能因为 TTL 过期而重新调用 LLM，必须等待原 lease、进入 retryable `parse_artifact_missing` 或人工修复。
- `action_execution_mode=on` 与 `shadow` 都由 Gateway 完成一次 parse 并把同一 parse 传给 Router；`shadow` 不 claim、不 finalize，也不改变用户可见回复，只通过 `legacy_from_parse` 生成兼容路由并记录差异。`off`、不在桶或 Gateway 预分类为 legacy 时，Gateway 不解析，Router 继续现有 legacy 单次解析。同一 turn 在任何模式最多一次 LLM parse，不能 Gateway 一次、Router 再一次。
- Gateway 与 Router 的 action 不一致时，先校验 session version/parse digest；若为可兼容映射，使用**同一 parse**走 `legacy_from_parse` adapter 并记录 `action_mismatch_fallback`；若无法安全映射，rollback Action、返回稳定澄清/稍后重试提示并标 retryable/terminal，禁止第二次 LLM parse、搜索或副作用。正常请求不得因再次分类而被改写为另一 action。

**具体代码改动范围**

- `backend/app/models.py`：为 `ActionExecution` 增加 `action_version`、`result_ref_type`、`request_id`、`snapshot_id`、`delivery_ids`（JSON）、`outbox_ids`（JSON）、`session_commit_id`、`result_schema_version`、`failure_code`、`replay_count`、`last_replayed_at`；增加只读 `ActionResultReference`（或等价独立表）时必须以 `action_execution_id + ref_kind + ref_id` 唯一。
- `backend/app/services/action_execution_service.py`：扩展 claim/finalize DTO；新增 `build_result_reference`、`load_replay_reference`、`mark_replay_started/finished`，所有更新带 owner+fencing 条件；禁止 helper 自行 commit。
- 新增 `backend/app/services/action_gateway.py`：实现上述纯预路由 `ActionEnvelope`、显式命令/轻量 classify、unknown/legacy 判定和严格超时；不得在 Gateway 内调用搜索、Session 写入或 Contact。
- `backend/app/services/intent_service.py`：新增 `classify_for_action_gateway` 单次解析适配器，返回 parse + legacy-compatible intent；禁止 Gateway 和 Router 各自调用 provider。
- `backend/app/models.py`：增加 `ActionParseArtifact` 映射，payload 仅允许 PII-free dialogue fields。
- `backend/app/services/recommendation_delivery_service.py`：让 `prepare_delivery` 返回稳定 delivery/outbox 引用并支持已存在行的幂等读取；补 `request_id/snapshot_id` 一致性校验。
- `backend/app/services/worker.py`：抽取 `_commit_action_result` 与 `_replay_committed_action`；保留现有 `_apply_session_commit_for_event`、`_deliver_outbox_for_event` 顺序。
- `backend/app/services/conversation_service.py`：为 staged commit 生成 durable commit id，并提供按 inbound/action 读取的恢复信息；不改变 Redis CAS API。
- `backend/app/services/message_router.py`、`backend/app/listing/search.py`：本阶段只增加可传递的 action context/result metadata，不在这里直接 claim。

**数据库/配置改动**

- 新增 `backend/sql/migrations/phase13_001_action_result_reference.sql`（仅 additive，含 `action_parse_artifact` 表、`action_execution.parse_ref/parse_digest/parse_version/parse_expires_at` 字段、索引 `action_execution(request_id,snapshot_id)` 和状态恢复索引）；旧列保留。
- 新增配置：`action_execution_mode=off|shadow|on`、`action_execution_rollout_percentage`、`action_execution_lease_seconds`、`action_replay_max_attempts`、`action_replay_stale_seconds`、`action_parse_cache_ttl_seconds=60`、`action_parse_artifact_retention_seconds=86400`；默认 `off`。

**事务、幂等、恢复边界**

- claim 必须发生在 `Worker._process_locked -> ActionGateway.classify -> claim -> message_router.process` 的固定边界；claim 事务只提交 Action lease，不提交 Router 的 staged session/业务写入。claim busy 时原 inbound 不得进入 Router。Gateway 超时且未生成 parse 时才走 legacy Router 单次解析；on/shadow 的 `none/unknown` 必须复用已生成的 parse_ref 走 `legacy_from_parse`/确定性 fallback，不得二次 LLM parse，也不在事后补 claim。
- on 路径的固定调用顺序为 `ActionGateway.classify_for_action_gateway (一次 parse) -> persist/read parse_ref -> claim -> message_router.process(action_context)`；shadow 路径去掉 claim/finalize 但仍传递同一 parse；Router 不得重新解析。Gateway/Router digest 或版本不一致时只能走同一 parse 的 `legacy_from_parse` 或安全澄清，不得再次调用 LLM。
- finalize 必须和结果事实/Outbox 同一 DB 事务；fencing 失败则整体 rollback，旧 Worker 不得写成功事实。
- DB commit 成功、Redis CAS 失败：标 inbound `session_pending`，reconciler 读取 durable commit；不再次调用 router。
- replay 发现 succeeded：校验 request/snapshot/delivery/outbox 行存在且属于同一 turn；只补投未发送 Outbox，并重试 Session CAS。不得调用 `search_service`、provider 或 rerank。

**新旧兼容**

旧 Action 行（无引用）保持 `failed_terminal`，在 `failure_code=legacy_unreplayable` 中标记并告警，不能自动重跑；搜索默认继续 legacy。旧 Worker 可读新增 nullable 列。

**测试用例**

- `backend/tests/unit/test_action_execution_service.py`：引用完整性、request digest 冲突、fencing 失败、重复 finalize、旧行不可 replay。
- 新增 `backend/tests/unit/test_action_gateway.py`：显式 `show_more`、pending relaxation、普通搜索、unknown、classifier 超时、off/legacy、claim busy/replay/action mismatch 路径。
- `backend/tests/unit/test_action_gateway.py`：补充 parse_ref TTL/cache miss、parse digest/schema/session version 绑定、on/shadow 的 `none/unknown` 复用 parse_ref、Gateway 与 Router 一致性及 `legacy_from_parse` fallback。
- 新增 `backend/tests/unit/test_action_replay_contract.py`：成功 replay 不调用 provider/rerank，不创建第二个 request/delivery/outbox。
- `backend/tests/integration/test_action_execution_replay_mysql.py`：DB commit 后 Redis 不可用、恢复顺序、并发抢占。
- 扩展 `backend/tests/integration/test_recommendation_session_outbox_consistency.py` 验证同事务引用。

**验收条件**

所有成功 Action 100% 可定位 request/snapshot；每个 on turn 在 Router 前恰好一次 claim 且最多一次 LLM parse；unknown/off/legacy turn 的 Action 覆盖率为 0；Router 与 Gateway action/parse digest 100% 一致；replay 结果与首轮 digest 相同；重复执行不新增推荐事实或 Outbox；Redis 故障恢复不重跑搜索；契约测试明确区分 `off` 与 `on`。

**灰度与回滚**

先 `shadow` 只记录引用完整性，再 1% worker 开 `on`；任何引用缺失、重复 delivery、CAS 恢复失败率超过阈值立即将 `action_execution_mode=off`，保留已提交行由 reconciler 处理。

### A1：初始 `search_job` 接入

**功能需求**

为 worker 的初始岗位搜索接入 `search_job` Action；相同 `turn_id` 重复投递只 replay，不能再次 SQL/rerank。权限拒绝和参数校验属于 terminal 结果，provider/Redis 暂时故障属于 retryable。

**当前代码现状**

`message_router._run_search` 在 `_job_search_facade_enabled` 时调用 `JobSearchFacade.search_jobs_v1`，否则调用 `search_service.search_jobs`；两者均无 Action。

**目标行为**

Worker 先经 `ActionGateway` 得到 envelope，再在 Router 前决定是否 claim；`search_job` acquired 才调用现有 Facade/legacy，claim busy 返回等待/重试且不运行路由，claim replay 走 `_replay_committed_action`。on/shadow 下普通自然语言若被 Gateway 判为 `unknown`，仍把同一 parse_ref 交给 `legacy_from_parse`/确定性 fallback，不再重新 classify；只有 off/不在桶才由 Router 自己做一次 legacy parse。搜索结果的 `request_id/snapshot_id` 被写入 Action reference，避免“执行完搜索才发现没 claim”。

**具体代码改动范围**

- `backend/app/services/message_router.py`：在 `_run_search` 接收可选 `ActionContext`，只负责返回 `SearchResult/Outcome` 和 reference metadata；保持 `_job_search_facade_enabled` 的 legacy fallback。
- `backend/app/services/message_router.py`：`process`、`_handle_text`、`_run_search` 接收 `ActionContext`；on 路径复用 Gateway 的 parse，不再调用 `classify_intent/classify_dialogue`，并提供 `legacy_from_parse` 兼容映射。
- `backend/app/listing/search.py`：`FacadeResult` 增加 `action_result_ref`，确保 cards 仅来自同一 snapshot；不引入 contact 解密。
- `backend/app/services/worker.py`：在 `_process_locked` 的 router 前后接入 A0 helper；action name 规范为 `search_job`，request digest 为规范化 criteria+profile+policy version。
- `backend/app/services/worker.py`：在 `_process_locked` 开头调用 `ActionGateway.classify`；按 envelope 分支执行 off/legacy、unknown、busy、replay 或 acquired，并把 context 传入 Router。
- `backend/app/services/worker.py`：保证 parse artifact 持久化/TTL 检查先于 claim，claim 成功后只把同一 `parse_ref` 传入 Router；重试优先复用 artifact，不重新 parse。
- `backend/app/services/search_service.py`：只补稳定结果 metadata/算法版本，不改变 SQL、rerank 或放宽逻辑。

**数据库/配置改动**

使用 A0 migration；增加 `action_execution_search_enabled` 细粒度开关（默认 false）和按角色/用户白名单配置。

**事务、幂等、恢复边界**

claim 事务与 Router 事务分离；finalize 失败时 rollback 全部搜索事实，旧 Worker 由 inbound retry 重新经过 Gateway 并按 lease/fencing 决定是否接管。finalize 成功后 provider 超时不触发 Router retry；由 durable Outbox/replay 负责。若 Gateway 判为 unknown/off，整个 turn 不写 Action，保留现有 legacy 行为。

**新旧兼容**

`off` 或不满足白名单继续原 `_run_search`；已有 `execution_mode=off/served_assignment=legacy` 事实不回填成 Action succeeded。

**测试用例**

扩展 `test_job_search_facade.py`、`test_search_service.py`、`test_worker.py`；新增预路由分类与 claim 顺序、重复 inbound、provider timeout、fencing steal、permission terminal、replay no-rerank、unknown/legacy fallback 的集成测试；增加 on/shadow/off 三种模式下“单 turn 最多一次 LLM parse”和“Gateway 分类与 Router 最终 action 一致”断言，并明确 on/shadow 的 `none/unknown` 不得触发第二次 parse。

**验收条件**

1% 灰度中 Action 覆盖率 100%，同一 turn 的搜索调用次数 ≤1，P95 不超过 legacy +10%，重复 Outbox/delivery 为 0，权限泄露为 0。

**灰度与回滚**

1% -> 5% -> 25% -> 50% worker 用户桶；监控 Action acquired/replay/busy、引用完整率、重复 provider 调用、session_pending、Outbox dead-letter。异常时开 kill switch，立即回 legacy。

### A2：`show_more` 快照 replay 接入

**功能需求**

`show_more_job` 只能消费原 snapshot 的剩余 IDs；重试/replay 不重新候选查询、不 rerank、不生成新 snapshot。耗尽是稳定 terminal 结果，可安全重复返回同一“没有更多”通知。

**当前代码现状**

`JobSearchFacade.show_more` 调 `search_service.show_more`，依赖 Redis Session 的 `candidate_snapshot/shown_items`；推荐事实的 `show_more` 已通过 `parent_request_id` 关联父 request。

**目标行为**

Action reference 保存父/子 request、snapshot、消费前后 cursor（shown count）和 delivery/outbox 引用。replay 先检查 delivery 是否已发送，再按 durable Session commit 恢复 cursor，不调用 `show_more`。

**具体代码改动范围**

- `backend/app/services/message_router.py` `_handle_show_more`：传入 action context，输出 cursor metadata。
- `backend/app/listing/search.py` `show_more`：返回 snapshot_id、page IDs、exhausted 标记；cards 必须由 snapshot 校验得到。
- `backend/app/services/conversation_service.py`：在 Session commit payload 中保存 cursor expected version/after state。
- `backend/app/services/worker.py`：实现 show_more replay 分支，优先按 inbound event 的 durable commit/outbox 恢复。

**数据库/配置改动**

无需新事实表；为 Action reference 增加 `cursor_before/cursor_after` JSON 校验字段；配置 `action_show_more_enabled` 默认 false。

**事务、幂等、恢复边界**

Session cursor 仍遵循 DB commit -> Redis CAS 两阶段；CAS 失败不重新分页。snapshot 过期或目标下架时 fail-closed，记录 terminal `snapshot_stale`，不换新候选池。

**新旧兼容**

legacy `show_more` 保持原文案和分页行为；旧 Session 无 snapshot 时继续既有“请先搜索”响应，不创建 Action succeeded。

**测试用例**

扩展 `test_recommendation_show_more_guard.py`、`test_job_search_v1_replay_contract.py`；新增跨 Worker 重放、cursor CAS 冲突、snapshot 过期、delivery 已发送/未发送两种恢复测试。

**验收条件**

同一 snapshot 的页序稳定、重复 turn 不增加 request/attempt；show_more P95 ≤1.5 秒；耗尽通知稳定；snapshot stale 不泄露已下架岗位。

**灰度与回滚**

仅对已启用 A1 的用户开放，1% -> 10% -> 50%；发现 cursor 回退、重复页或新候选查询立即关闭 `action_show_more_enabled`，保留 A1/legacy 初始搜索。

### A3：relaxation 单步接入

**功能需求**

`relax_job` 只执行 Session 中已确认且未过期的一个 step；同一 turn 重试复用最终 relaxed request/snapshot，不重新 probe 或再次放宽。拒绝/过期/不匹配为 terminal。

**当前代码现状**

`message_router._route_v2_relaxation_response` 校验 `pending_relaxation` 后调用 `JobSearchFacade.relax_search` 或 `search_service.execute_relaxed_search`；`search_service` 已有 `relax_probe_results` 和 `confirmed_relaxed` 事实。

**目标行为**

Action reference 同时保存原 criteria digest、step、probe request IDs、最终 served request/snapshot；replay 只使用最终事实，不重新计算 `_compute_relaxed_criteria_*`。

**具体代码改动范围**

- `backend/app/services/message_router.py`：为确认放宽分支传递 `ActionContext`，将 `pending_relaxation` 校验结果编码到 request digest。
- `backend/app/listing/search.py` `relax_search`：返回 `confirmed_relaxed` metadata，保持“一次一步”校验。
- `backend/app/services/search_service.py`：补稳定 attempt 引用和 probe digest；不允许外部传入 `relaxed_criteria`。
- `backend/app/services/worker.py`：增加 `relax_job` replay/fencing 分支。

**数据库/配置改动**

增加 `action_relax_enabled`（默认 false）；Action reference 增加 `relax_step`、`probe_request_ids`。

**事务、幂等、恢复边界**

probe 与 served attempt 必须在同一个 Action 结果事务落库；finalize 后 CAS 失败只恢复 Session/outbox。第二次收到确认消息因不同 `turn_id` 视为新 Action，但 reducer 必须清掉旧 pending，不能自动连放第二步。

**新旧兼容**

`post_search_policy_mode=off` 和未启用 Action 时沿用 legacy/现有 reducer 结果；不把 shadow probe 当用户可见结果。

**测试用例**

扩展 `test_post_search_reducer.py`、`test_post_search_applier.py`、`test_phase5_4_soft_pref_notice.py`；新增重复确认、过期确认、同 step 并发、probe 超时、replay 不再 probe 测试。

**验收条件**

每个确认 turn 最多一个 served relaxed attempt；原/放宽 criteria 与审计一致；重复消息无重复 rerank/delivery；用户可见结果和 legacy golden replay 一致。

**灰度与回滚**

仅在 A1/A2 稳定桶中启用，按 `post_search_policy_mode` 的 `shadow -> on` 推进；任何二次放宽、probe 重跑或状态污染立即关闭 `action_relax_enabled`。

### A4：A 线生产完成门禁

**功能需求**

统一 dashboard、告警、reconciler 和 replay CLI；把 Action 失败分类、fencing 丢失、结果引用缺失、session_pending、Outbox 投递和重复 provider 调用纳入 SLO。

**当前代码现状**

已有 `worker_monitor`、推荐 delivery dispatcher、session reconciler 和大量 integration tests，但没有 Action 维度的统一指标和演练脚本。

**目标行为**

任何已提交 Action 都能在 10 分钟内完成 Session/Outbox 恢复或进入可解释 terminal；P0/P1 自动 kill switch。

**具体代码改动范围**

- `backend/app/tasks/worker_monitor.py`：增加 Action stale lease、引用缺失、replay backlog 检查。
- `backend/app/services/worker.py`：补 replay metrics 和结构化事件。
- 新增 `backend/scripts/action_execution_preflight.py`、`backend/scripts/action_execution_emergency_rollback.py`。
- `backend/tests/rollout/test_action_execution_rollout_gates.py`：固化门禁。

**数据库/配置改动**

增加告警阈值配置：引用缺失=0、重复 provider 调用=0、P1 session_pending 超时 <0.1%、Action stale >5 分钟告警；所有阈值可动态关闭新路由。

**事务、幂等、恢复边界**

执行 DB/Redis/provider/WeCom 四类故障演练；reconciler 必须可重复执行，不能通过重跑 router 修复。

**新旧兼容**

legacy 与 on 的 golden case 差异只允许出现在已批准文案范围内；旧 Action 行继续只读可审计。

**测试用例**

新增四类故障 chaos、指标告警、replay CLI 幂等和 legacy parity 测试。

**验收条件**

7 天观察窗口无 P0/P1，Action replay 成功率 ≥99.9%，Outbox 最终投递 ≥99.9%，session_pending 超时率 <0.1%。

**灰度与回滚**

按 1% -> 5% -> 25% -> 50% -> 100% 推进；回滚为配置切换到 off/legacy，禁止删除 Action 或推荐事实。

## 4. Workstream B：Contact Domain Service 与 PII 闭环

> 实施状态：B0-B4 已完成工程实现与验证。Contact request/grant/delivery、PII ciphertext、加密回填和 Outbox 分流已落地；迁移状态为 `completed`，Contact 默认仍 feature-off，待生产灰度和清理审批。

### B0：数据分类、接口和 feature-off 基线

**功能需求**

定义 `ContactRequest`（入口）与 `ContactGrant`（兑换凭据）边界；所有 contact API 服务端重新解析 actor、listing、当前版本、审核/有效期、角色、黑名单和策略。未完成服务时所有兑换返回稳定的“暂不可用”，不返回 phone。

**当前代码现状**

原基线是通过 `_opaque_contact_id` 生成 hash ID，尚无 Contact Service。当前实现已改为真实 SQLAlchemy Session 下由 `ContactService.create_contact_request()` 持久化 request；Router 的显式“联系”分支会按当前搜索快照重新校验岗位并执行 authorize -> issue grant -> redeem。

**目标行为**

Card 只含 opaque request；Contact Service 是唯一能读取/解密联系方式的模块。request 不携带明文或可逆 token，兑换必须绑定 actor+listing+action+nonce。

**具体代码改动范围**

- 新增 `backend/app/listing/contact.py`：`create_contact_request`、`authorize_contact`、`issue_one_time_grant`、`redeem_grant`、`revoke_grant`、`audit_contact_event`。
- 新增 `backend/app/schemas/contact.py`：输入/输出 DTO 只允许 opaque IDs、token metadata 和受控 contact channel。
- `backend/app/listing/search.py`：真实 Session 下调用 Contact Service 创建 request；保留字段名 `contact_request_id`，不生成 phone。
- `backend/app/listing/render.py`、`backend/app/services/recommendation_delivery_service.py`、`backend/app/services/worker.py`：增加静态 PII 断言/投影过滤。

**数据库/配置改动**

新增 `backend/sql/migrations/phase13_010_contact_core.sql`：`contact_request`、`contact_grant`、`contact_access_audit` 表，均含 actor/listing/action digest、状态、expires/revoked/used_at、策略版本和 trace id；不存 token 明文，只存 hash。配置 `contact_service_mode=off|shadow|on`（默认 off）、`contact_grant_ttl_seconds=60`。

**事务、幂等、恢复边界**

request 创建与搜索事实同一 DB 事务可关联，但 token 签发/兑换独立短事务；兑换以 `used_at IS NULL` 的条件更新保证一次性。服务异常 fail-closed；审计写失败不得泄露联系方式。

**新旧兼容**

旧 `contact_request_id` 继续出现在 Card；旧入口不自动兑换明文。历史已发 Card 只能得到“请重新请求联系”的兼容提示。

**测试用例**

新增 `backend/tests/unit/test_contact_service.py`、`backend/tests/unit/test_contact_schema.py`、`backend/tests/integration/test_contact_grant_mysql.py`；扩展 `test_job_search_v1_replay_contract.py` 验证 trace 无 PII。

**验收条件**

Contact mode off 时兑换成功率为 0 且无明文 fallback；服务端能定位 request/audit；token hash 不可逆，跨 actor/listing 兑换全部失败。

**灰度与回滚**

先 shadow 校验请求和策略但不发 token；任何异常保持 off。回滚只关闭 `contact_service_mode`，不删除 request/audit。

### B1：PII 加密存储与密钥治理

**功能需求**

phone、微信号及联系人敏感值使用 AEAD 加密，密钥版本化、可轮换；索引只允许不可逆 digest（仅用于精确匹配/迁移核对），禁止全文索引。

**实施前基线（已完成替换）**

`User.phone`、`Job.phone`、`contact_person` 是 VARCHAR 明文；推荐 delivery content 已有 AES-GCM 辅助，但未覆盖事实源 PII。

**目标行为**

Contact Service 读取加密列并在授权内存中解密；普通 ORM 查询、搜索投影、Prompt builder、日志 serializer 默认拿不到明文。

**具体代码改动范围**

- `backend/app/models.py`：为 User/Job 增加 `phone_ciphertext`、`phone_key_version`、`phone_digest`、`wechat_ciphertext` 等 nullable 列；保留旧列只读过渡。
- 新增 `backend/app/services/pii_crypto_service.py`：复用项目 AEAD 约定，支持 active/previous key、AAD、轮换和零化生命周期。
- `backend/app/services/account_service.py`、`upload_service.py`：写入改为 Contact/PII service，禁止直接赋值旧明文列。
- `backend/app/services/search_service.py`、`visibility_policy.py`、`llm/base.py`、`llm/prompts.py`：从允许字段注册中移除 phone/wechat/contact_person 原值，只保留存在性/脱敏占位。

**数据库/配置改动**

新增 `phase13_011_contact_pii_ciphertext.sql` 和密钥配置 `pii_active_key_version`、`pii_keyring_ref`、`pii_migration_batch_size`；迁移脚本必须可暂停、可重跑。

**事务、幂等、恢复边界**

单行加密回填以主键 checkpoint 和版本条件更新；密钥不可用时停止回填和 Contact on，不影响搜索 legacy。写新列成功后才标记迁移状态，不覆盖原值。

**新旧兼容**

旧明文字段在过渡窗口只读；读取优先 ciphertext，缺失时仅在迁移 worker 内受控读取旧列，绝不向业务层返回裸值。未完成迁移的记录 Contact 请求 fail-closed。

**测试用例**

新增 `test_pii_crypto_service.py`、`test_contact_pii_migration_mysql.py`；扩展 `test_visibility_contract.py`、`test_llm_call_policy.py`、`test_recommendation_plaintext_redaction.py`，并做 key rotation/损坏密文测试。

**验收条件**

新写入 PII 明文列为 NULL；ciphertext 可解密且 key version 正确；Prompt/index/Card/Outbox/log 静态扫描无 phone/wechat 值；迁移 checkpoint 可恢复。

**灰度与回滚**

先 1% 新写入双读 shadow（不双写明文），再全量新写；解密错误率>0 或密钥告警立即关闭 Contact on。旧列保留至 B3 清理签字，不回滚已加密数据。

### B2：一次性 token、频控、撤销和审计

**功能需求**

- grant TTL 默认 60 秒、单次兑换；绑定 `actor_id/listing_ref/action/nonce/policy_version`。
- 频控默认每 actor+listing 每 10 分钟最多 3 次、每 actor 每日最多 30 次，阈值配置化；Redis 计数器仅作前置限流，最终以 DB audit/状态为准。
- 下架、审核撤销、封禁、策略版本变化即时使未兑换 grant 失效；所有成功/失败都审计原因码。

**当前代码现状**

原基线没有 token API 和 Contact Outbox 分支。当前实现已增加 `contact_delivery_id`，Worker discovery/claim/send/recovery 全部按 ContactDelivery 独立分支处理，platform_request 使用固定无 PII 模板，真实联系方式仍 fail-closed 要求 ciphertext。

**目标行为**

`redeem_grant` 每次重新锁定 Listing/owner/contact 记录并校验当前版本；任何 stale/越权/超频均返回统一失败，不暴露具体存在性。

**Contact delivery 线性化与可重试凭据**

v1 采用“grant 一次消费、delivery 稳定重试”的模型，不在重试时重新兑换 grant：

1. `redeem_grant` 在一个短 DB 事务内按 `grant_id` 加行锁，重新鉴权 actor/listing/version/policy，并以 `used_at IS NULL AND revoked_at IS NULL AND expires_at > now()` 条件更新 grant；并发请求只有一个更新成功。
2. 同一事务创建唯一的 `contact_delivery(delivery_id, grant_id, actor_id, listing_ref, channel, content_ciphertext, key_version, content_hash, expires_at, status, revoked_at)`。`delivery_id` 由服务端生成，`grant_id` 唯一约束保证不会产生第二个 delivery；ciphertext 是短期、加密的受控联系方式或平台联系请求 payload，明文不落库。
3. 同一事务写入无 PII 的 Outbox 行（只含 `delivery_id`、模板 key、目标 userid 和 trace），然后提交 `grant.used_at`、delivery 和 Outbox。这个 DB commit 是“授权已消费且可投递”的唯一线性化点。
4. Outbox dispatcher claim 后，按 `delivery_id` 锁定并检查 delivery 未过期/未撤销，从 ciphertext 在进程内解密一次，调用 provider；不读取或重新兑换 grant。provider 成功后只更新 delivery/outbox 的 sent 状态。
5. provider 超时、连接断开或响应丢失：delivery 保持 `prepared/sending`，Outbox 按原 delivery_id 重试，复用同一 ciphertext/content_hash 和模板；绝不生成新 grant、新 delivery 或重新执行权限流程。若 provider 不支持幂等，外部消息仍可能 at-least-once 重复，但重复内容来自同一授权 delivery，必须通过 provider_msg_id/发送审计标注，不得通过二次兑换规避。
6. 撤销发生在发送前：事务将 delivery/outbox 标记 `revoked`/`dead_letter`，dispatcher 拒绝解密和发送。撤销发生在 provider 已确认发送后：只能阻止后续重试并记录不可逆的 `sent_after_revoke` 审计，不能假装撤回已到达的消息。delivery 到期同样 fail-closed。

若产品不允许发送任何联系方式，则 `channel=platform_request`，ciphertext 只保存平台联系请求 payload；上述 grant/delivery/Outbox 线性化和重试规则不变。无论哪种 channel，phone/微信号都不进入 Outbox、Card、Prompt、索引或普通日志。

**具体代码改动范围**

- `backend/app/listing/contact.py`：实现授权、签发、兑换、撤销状态机。
- `backend/app/listing/contact.py`：实现上述 grant -> delivery 原子事务；新增 `load_delivery_for_send`，只返回受控短生命周期解密句柄，不返回给 Router。
- `backend/app/models.py`：增加 `ContactDelivery` ORM 映射及 `grant_id` 唯一约束对应的关系字段；在现有 `WecomOutboundOutbox` 增加 nullable `contact_delivery_id`，并加入“`recommendation_delivery_id` 与 `contact_delivery_id` 不得同时非空”的 check/invariant、`uk_outbox_contact_delivery` 唯一约束和 `idx_outbox_contact_delivery(status, contact_delivery_id, id)` 索引；模型层禁止暴露明文便利属性。
- `backend/app/services/permission_service.py`、`visibility_policy.py`：提供 contact-specific policy check，不复用搜索可见性结果。
- `backend/app/services/worker.py`、`wecom_client.py`：dispatcher 按 `contact_delivery_id` 分支取加密 delivery、内存解密并发送；Outbox 仅存模板和 delivery reference，不存明文或 grant 原文。
- `backend/app/services/worker.py`：扩展 `_build_outbox_claim_query`、`_claim_outbox_candidate`、`_deliver_outbox_item` 和 stale-claim recovery：`contact_delivery_id` 行只锁/校验 `ContactDelivery`，不 join/调用 `RecommendationDelivery`；推荐行继续走原 recommendation lock/status 机，普通文本行两者均为空。
- `backend/app/tasks/worker_monitor.py`：增加 grant expiry/revoke cleanup 与频控异常指标。

**数据库/配置改动**

为 contact 表增加 `listing_version`、`policy_digest`、`revoked_at/revoke_reason`、`rate_bucket`；新增 `contact_delivery` 表及 `uk_contact_delivery_grant`、`idx_contact_delivery_due`；修改 `wecom_outbound_outbox` 增加 `contact_delivery_id`（FK/软 FK 指向 `contact_delivery.delivery_id`）、`uk_outbox_contact_delivery`、`idx_outbox_contact_delivery` 和互斥 invariant；配置 `contact_rate_per_listing_window/limit`、`contact_daily_limit`、`contact_revoke_on_version_change`、`contact_delivery_ttl_seconds`。

**事务、幂等、恢复边界**

兑换事务顺序固定为 actor policy -> listing/version -> grant 条件更新 -> contact_delivery insert -> audit -> 写入带 `contact_delivery_id` 的无 PII Outbox；任何一步失败 rollback，唯一约束处理并发。发送异步化以 grant.used_at + ContactDelivery + contact Outbox 同一 DB commit 为线性化点；provider 超时由同一 delivery 的 Outbox retry，不能重新兑换 grant。撤销/过期在每次 contact outbox dispatcher claim 前重新检查。

**现有 Outbox claim/lock/status 识别规则**

- `_build_outbox_claim_query` 先按 `recommendation_delivery_id IS NOT NULL`、`contact_delivery_id IS NOT NULL`、两者均为空分类；contact 分支只锁 `ContactDelivery`（稳定 `delivery_id` 顺序）再锁 Outbox，使用现有 pending/sending lease/status 机，不调用推荐目标校验。
- `_claim_outbox_candidate` 将 claim 类型写入内存 item（`delivery_kind=contact|recommendation|plain`）；contact item 只允许 `contact_delivery_id`，若同时出现两个 ref、ref 不存在、已撤销/过期或状态不一致则 rollback 并 dead-letter，不能降级到推荐分支。
- `_deliver_outbox_item` 对 `delivery_kind=contact` 调 `ContactService.load_delivery_for_send(contact_delivery_id)`，在进程内短暂解密并调用 provider；成功/失败只更新 ContactDelivery 与该 Outbox 的状态和 lease。`recommendation_delivery_id` 路径完全复用现有推荐锁顺序，二者不共享 delivery 表状态。
- `_recover_stale_outbox_claims`、`_mark_outbox_sent/failed` 和 session/outbox reconciler 必须按 `contact_delivery_id` 更新对应 delivery；contact outbox 恢复不得创建 `RecommendationDelivery`、推荐事实或新的 grant。`inbound_event_id + reply_index` 仍是 Outbox 通用幂等键，`uk_outbox_contact_delivery` 防止同一 delivery 生成第二条 contact outbox。

**新旧兼容**

旧 Card 文案仍可提示“回复联系”，但没有 Contact Service 时始终返回 feature-off 文案；不把旧 `phone` 直接放回响应。

**测试用例**

新增 token replay、跨 actor、跨 listing、过期、撤销、版本变化、频控边界、Redis 限流器失效 fail-closed、并发兑换只有一个 delivery、provider timeout/retry 复用同一 delivery、发送前撤销阻断、发送后撤销禁止二次发送测试；集成测试覆盖 grant used_at 与 delivery/outbox 同事务提交。

**验收条件**

一次性兑换成功率与 `used_at`/delivery 状态严格一致；每个 grant 最多一个 delivery；越权/跨租户/已下架泄露为 0；频控边界符合配置；审计覆盖率 100%；Outbox 重试只使用同一 delivery，不重复兑换、不生成新授权、不暴露明文；发送前撤销必阻断。

**灰度与回滚**

1% actor 灰度，观察 24 小时后 10%/50%；撤销事件延迟 P99 <30 秒。异常时关闭兑换开关，已签发 grant 全部可批量 revoke，搜索继续可用。

### B3：PII 回填、旧列冻结与清理门槛

**功能需求**

分批加密回填 User/Job 历史 phone、联系人和微信号；每批校验计数、digest、解密抽样、失败重试和 checkpoint。完成后旧列只读，再经合规批准清空/删除。

**当前代码现状**

phase10 旧字段已存在且被 account/upload/search 测试和后台使用，没有迁移状态表。

**目标行为**

迁移 worker 与线上写入互斥通过版本条件控制；任何密钥/数据异常自动暂停，不影响 legacy 搜索（但 Contact 对未迁移记录保持 off）。

**具体代码改动范围**

- 新增 `backend/scripts/contact_pii_backfill.py`、`backend/scripts/contact_pii_verify.py`、`backend/sql/migrations/phase13_012_contact_pii_backfill_state.sql`。
- `backend/app/api/admin/*` 仅增加迁移进度/二次授权审计，不返回批量明文。
- 更新 `backend/tests/integration/test_privacy_redaction_batch_mysql.py` 覆盖 pause/resume、重复运行和旧列冻结。

**数据库/配置改动**

增加 `contact_pii_migration_state`（entity、last_pk、success_count、error_count、key_version、status）；旧列清理 migration 单独审批，默认不执行。

**事务、幂等、恢复边界**

每批小事务、可重跑；不得使用全表锁。校验失败只暂停该 entity，保留原值供人工核查；清理前必须有可验证备份/保留策略。

**新旧兼容**

双读窗口内优先 ciphertext；旧列只读，Contact API 不返回旧明文。

**测试用例**

更新 `backend/tests/integration/test_privacy_redaction_batch_mysql.py`，覆盖 pause/resume、重复运行、旧列冻结和并发线上写入。

**验收条件**

100% 可迁移记录有 ciphertext，抽样 100% 解密正确，旧明文写入路径为 0，checkpoint 可审计。

**灰度与回滚**

按 entity 逐批放量；失败只暂停当前 entity。回滚停止 worker 并恢复只读旧列读取（仅迁移运维路径），不把旧明文重新暴露给 Contact API。

### B4：B 线生产完成门禁

**功能需求**

建立 PII 数据流清单、静态扫描规则、密钥轮换演练、Contact SLO 和泄露告警；所有 contact success/fail/revoke 可按 trace 查询但日志只含 hash/原因码。

**当前代码现状**

已有推荐隐私清理、投影和日志脱敏测试，但没有 Contact 专属 dashboard 和泄露阻断。

**目标行为**

所有 contact success/fail/revoke 可按 trace 查询但日志只含 hash/原因码；P0/P1 泄露自动关闭 Contact 开关。

**具体代码改动范围**

`backend/app/tasks/worker_monitor.py`、日志/指标配置和安全扫描规则；补充 Contact dashboard 与告警 runbook。

**数据库/配置改动**

增加 Contact SLO、PII 扫描和 token replay 告警阈值；不新增明文列。

**事务、幂等、恢复边界**

告警/指标写失败不得阻塞或绕过授权；revoke、audit、migration reconciler 可重试且不重复发 grant。

**新旧兼容**

legacy 搜索和历史 Card 继续可读，但不能兑换明文联系方式。

**测试用例**

加入安全扫描、告警触发、密钥轮换和 7 天 replay fixture 验证。

**验收条件**

7 天观察窗口内 token 重放成功=0、跨 actor 泄露=0、PII 出现在 Prompt/index/Card/Outbox/log=0；grant 兑换 P95 <500ms，频控和撤销 P99 <30s；安全负责人签字后才可将 `contact_service_mode` 默认设为 on。

**灰度与回滚**

Contact 独立 kill switch 优先级高于任何 facade 开关；回滚关闭兑换、revoke 未使用 grant、保留审计和加密数据，搜索与 legacy 回复不受影响。

## 5. Workstream C：灰度、故障演练与 legacy 退出

> 实施状态：C1/C2/C4 的观测、故障矩阵和回滚工具已完成，C2 九类场景全部通过；C3 的 14 天生产指标窗口和退出签字尚未满足，因此 legacy 仍保留为默认路径。

### C1：观测与停止条件

**功能需求**

统一 Action、搜索、Session、Outbox、Contact 指标和 PII 静态扫描。

**当前代码现状**

已有 worker/recommendation/session 指标，缺少 Action/Contact 统一维度。

**目标行为**

统一指标维度：`turn_id_hash`、action、profile、execution mode、assignment、request/snapshot digest、fencing token（不可记录原文/phone）。核心指标包括：Action claim/replay/busy/terminal、引用完整率、provider 调用次数、rerank 重复率、session_pending backlog、Outbox pending/dead-letter、Contact grant success/replay/revoke/rate-limit、PII 静态扫描命中。

停止条件：任何 P0/P1 泄露、重复业务写入、同 turn provider 调用>1、Action 引用缺失、Contact token 重放成功、session_pending 超时率>0.1% 或 P95 超过预算 20%，立即切对应 kill switch。

**具体代码改动范围**

`backend/app/services/worker.py`、`backend/app/tasks/worker_monitor.py`、指标/告警配置和 dashboard。

**数据库/配置改动**

新增 Action/Contact 维度和阈值配置，默认只读观测。

**事务、幂等、恢复边界**

指标重复上报不影响业务；告警触发只切路由，不删除事实。

**新旧兼容**

继续保留 legacy/fallback 指标，不能把 legacy 流量误算为 on。

**测试用例**

新增 rollout gate 单测、指标 cardinality/脱敏测试和告警模拟。

**验收条件**

所有停止条件可在 5 分钟内观察并触发对应开关。

**灰度与回滚**

先 shadow，再按 A/B 阶段独立放量；告警触发立即关闭单条 workstream。

### C2：故障演练矩阵

**功能需求**

每月执行 DB、Redis、provider、WeCom、密钥和并发抢占演练。

**当前代码现状**

已有 session/outbox/recommendation 集成测试，尚无统一 Action/Contact 演练入口。

**目标行为**

| 场景 | 预期结果 | 禁止结果 |
|---|---|---|
| claim 后 Worker 崩溃 | lease 到期后新 Worker 接管一次 | 两个 Worker 都 finalize |
| provider/rerank 超时 | retryable 或 legacy fallback；无成功事实 | 超时后重复 rerank/重复 delivery |
| DB commit 后 Redis CAS 失败 | `session_pending`，reconciler 完成 CAS/outbox | 重跑 router 或重复搜索 |
| Outbox HTTP 响应丢失 | 既有 sending lease/retry 机制处理 | 再次生成搜索结果或 Contact grant |
| snapshot 过期/岗位下架 | fail-closed，记录 terminal | 展示 stale 岗位/换新候选池 |
| 密钥不可用/密文损坏 | Contact off，迁移暂停，搜索可用 | 明文 fallback |
| Redis 频控不可用 | Contact 兑换 fail-closed 或 DB 安全上限 | 放行无限请求 |
| grant 已消费、provider 超时 | 同一 `contact_delivery` 保持 pending，Outbox 重试同一 delivery | 二次兑换 grant 或生成第二份 payload |
| delivery 已创建后撤销 | 发送前阻断并 dead-letter；发送后禁止后续重试并记审计 | 撤销后继续发送或伪造撤回成功 |

演练脚本放在 `backend/scripts/`，每月一次；演练数据使用 mock provider 和脱敏 fixture。

**具体代码改动范围**

新增 `backend/scripts/action_contact_chaos.py` 和对应 rollout fixtures；复用现有 reconciler。

**数据库/配置改动**

演练环境使用独立 schema、短 lease/TTL 和 mock provider 配置。

**事务、幂等、恢复边界**

每个场景必须验证 fencing、DB/CAS 两阶段和 Outbox 恢复，禁止用人工补写掩盖重复副作用。

**新旧兼容**

演练期间 legacy 流量保持可用，结果不写生产事实。

**测试用例**

矩阵中的每行至少有自动化断言和人工 runbook 步骤。

**验收条件**

所有场景达到表中预期结果，且恢复时间在既定 SLO 内。

**灰度与回滚**

先 staging，再小流量生产；发现不可恢复状态立即回退到 legacy 并隔离演练数据。

### C3：legacy 退出门槛

**功能需求**

定义 legacy 只读、停止新流量、删除不可达代码的分阶段门禁。

**当前代码现状**

`job_search_facade_enabled`、推荐 execution mode 和 Contact mode 均支持 off，但 legacy 仍是默认安全路径。

**目标行为**

只有在 A4、B4 完成并满足以下条件后，才能提出退出 legacy 的 RFC：连续 14 天 Action on 覆盖率 ≥99%、replay/恢复成功率 ≥99.9%、重复 provider 调用=0；Contact 连续 14 天 PII 泄露=0、token replay=0；所有 golden replay 与旧链路差异有批准记录；后台、旧 Session 和历史 Card 均有兼容策略。

退出步骤：先停止新流量进入 legacy（保留只读/紧急回退 30 天），再删除不可达分支和旧明文写入路径；任何删除需单独 migration 和数据保留审批。本方案不执行退出。

**具体代码改动范围**

另立 RFC 后再改 `message_router.py`、Facade adapter、旧 PII writer 和后台兼容层；本方案只提供 gate 测试。

**数据库/配置改动**

只增加 rollout history/approval 记录，不删除旧列或旧事实表。

**事务、幂等、恢复边界**

退出前必须完成所有 pending Action/Session/Outbox；退出期间仍可按 turn replay，不能重跑业务。

**新旧兼容**

保留历史 Session/Card/后台读取兼容至少 30 天。

**测试用例**

新增 legacy parity、历史 Session、旧 Card 和 emergency rollback 测试。

**验收条件**

满足 14 天指标门槛、审批记录齐全、紧急回退演练通过后才可停止新 legacy 流量。

**灰度与回滚**

按“停止新流量 -> 只读保留 -> 删除代码”的顺序推进；任何指标回退均恢复 legacy 路由。

### C4：统一回滚 runbook

**功能需求**

提供 Action、Contact、Facade 各自独立且可组合的紧急回滚步骤。

**当前代码现状**

各模块已有零散 kill switch/reconciler，没有统一 incident 顺序。

**目标行为**

回滚顺序固定为：1) 关闭 Action/Contact on；2) 将 facade/策略切回 legacy；3) 保留并扫描已提交 Action、推荐事实、audit、Outbox；4) 运行 session/outbox reconciler；5) 核对无重复发送/PII 泄露；6) 生成 incident report。禁止 `git revert`、删除事实表或清理审计作为回滚手段。

**具体代码改动范围**

新增 `backend/scripts/action_execution_emergency_rollback.py`、Contact revoke 命令和 incident checklist。

**数据库/配置改动**

仅更新开关、revoke 状态和审计记录；不做破坏性 migration。

**事务、幂等、恢复边界**

回滚命令可重复执行；reconciler 按 lease/fencing 和 Outbox 幂等恢复。

**新旧兼容**

legacy 路由和旧 Session 继续受理；已加密 PII 不回退为 API 明文。

**测试用例**

演练重复执行、部分失败、命令中断后的续跑和审计完整性。

**验收条件**

15 分钟内完成开关切换，恢复后无新增重复业务写入或 PII 暴露。

**灰度与回滚**

runbook 先 staging 演练后生产值班演练；回滚本身失败时保持全局 off 并升级人工处理。

## 6. 交付清单与评审重点

### 6.1 交付文件清单

- 文档：`docs/architecture/10-post-v1-action-contact-implementation-plan.md`。
- A 线预期代码/迁移/测试：`backend/app/services/action_execution_service.py`、`backend/app/services/action_gateway.py`、`backend/app/services/intent_service.py`、`backend/app/services/worker.py`、`backend/app/services/message_router.py`、`backend/app/listing/search.py`、`backend/app/services/conversation_service.py`、`backend/app/services/recommendation_delivery_service.py`、`backend/app/models.py`、`backend/sql/migrations/phase13_001_action_result_reference.sql`、`backend/scripts/action_execution_preflight.py`、`backend/tests/unit/test_action_gateway.py`、`backend/tests/unit/test_action_replay_contract.py` 及对应 MySQL/rollout 测试。
- B 线预期代码/迁移/测试：`backend/app/listing/contact.py`、`backend/app/schemas/contact.py`、`backend/app/services/pii_crypto_service.py`、`backend/app/services/worker.py`（delivery dispatcher）、`backend/sql/migrations/phase13_010_contact_core.sql` 至 `phase13_012_contact_pii_backfill_state.sql`、`backend/scripts/contact_pii_backfill.py`、`backend/tests/unit/test_contact_service.py` 及对应 grant/delivery 并发、重试、撤销和隐私集成测试。

以上文件范围已按 A/B/C 计划完成工程交付；生产 rollout、长期观察和 legacy/旧明文清理仍受第 0 节和第 5 节门禁约束。

### 6.2 评审必须重点关注的风险

1. **DB commit 与 Redis CAS 两阶段边界**：Action finalize、durable Session commit、replay 和 `session_pending` 是否始终可判定，是否存在“成功但无引用”或旧 Worker 越过 fencing 的窗口。
2. **结果引用与重复副作用**：`request_id/snapshot_id/delivery/outbox` 是否足以重建每种 action；provider 超时、Outbox 响应丢失和 show_more/relaxation 重试是否绝不重新 rerank 或重复发送。
3. **PII 泄露路径**：phone/微信号是否仍可能从 ORM、Prompt、索引、Card、ConversationLog、RecommendationDelivery 或普通日志旁路流出；加密迁移失败时是否真正 fail-closed。
4. **撤销与频控的一致性**：Listing 版本、封禁、策略变化和 Redis 不可用时，token 是否即时失效且不会因缓存/并发兑换绕过限制。
5. **legacy 与灰度可回退性**：开关粒度、指标告警和 runbook 是否能只关闭问题 workstream，并保留已提交事实、审计和恢复队列；是否有明确 owner 和签字门槛。
