# 对话核心链路生产化加固计划

版本：v0.3（持久会话提交收口版，2026-07-25）
基线：`edd5183`（`codex/phase5-recommendation-experience`）
范围：文本发布岗位/简历、多轮补字段/取消/冲突切换、搜索推荐/翻页/自动放宽、未知表达/复杂组合意图、高并发生产运行。
明确非目标：图片、文件、语音上传。

## 1. 结论与生产准入判断

本轮工程改造已经完成，当前代码可判定为“具备预生产/小流量灰度条件”，文本对话范围内的功能正确性、故障恢复和短时容量验证均已通过。不能把该结论扩大为“已经完成生产全量验收”：持续容量、真实历史语料、生产 shadow 观察周期仍需在真实流量环境完成。

已经闭环的关键项包括：草稿非显式保留、锁自动续租和 fencing、session version CAS、同用户持久化顺序门禁、MySQL 持久会话提交意图与 Redis 幂等恢复、reranker 全异常降级、legacy/v2 放宽一致性、受限两动作计划、动态工种 ontology、显式城市/工种锚点、broker 双方向隔离、角色方向后端约束、数据库恢复扫描、事务出站箱、日志脱敏、队列/处理/outbox/session-commit 监控以及可重复执行的真实环境烟测、回放和负载脚本。

正式全量生产前仍必须完成 §9 的环境性门槛。特别说明：企微 `message/send` 不支持客户端幂等键；事务出站箱能保证业务路由不重跑、回复意图不丢，但在“企微已接收、HTTP 响应丢失、Worker 随后重试”的极端窗口仍可能重复发送，不能宣称端到端严格 exactly-once。

## 2. 生产级完成定义

### 2.1 正确性不变量

- 未收到显式取消、草稿 TTL 到期或合规删除指令时，不得清空上传草稿。
- 同一用户消息必须按服务端接收顺序串行提交 session；任意模型慢调用期间不得出现两个 Worker 同时持有有效处理权。
- 所有 LLM 输出必须经过 schema、角色权限和 reducer；LLM 不直接写 session。
- 搜索硬条件不得被静默移除。用户本轮刚声明的字段若需要放宽，必须先确认。
- 一次放宽最多执行一个明示步骤；二次 reducer 禁止继续自动放宽。
- 翻页只能消费当前快照，不能重复展示，且必须使用快照的 `effective_criteria` 解释结果。
- 排序服务失败不得导致已有 SQL 候选不可用；必须退回稳定的确定性顺序并记录降级事件。
- 命令、取消、重置、冲突确认等高风险动作必须有确定性闭集保护，不依赖开放式关键词包含匹配。

### 2.2 语言能力目标

“足够通用”定义为招聘域内的受支持能力矩阵，而不是任意自然语言代理：

- 三角色：worker / factory / broker。
- 四类主 frame：岗位发布、简历发布、岗位搜索、工人搜索。
- 支持单轮多槽、跨轮补槽、同字段替换/追加/删除、方向切换、取消/重置、上传与搜索冲突、翻页、低召回放宽确认。
- 支持常见口语、省略、倒装、错别字、否定、反悔、同义词和短回答。
- 第一阶段复杂组合支持上限：一个主动作 + 一个可延后动作；超过上限时必须澄清，不猜测执行顺序。
- 指代只支持可验证锚点（最近展示列表中的序号/明确 ID）；“这个/那个”存在多个候选时必须反问。
- 不支持的行业字段或动作必须明确说明边界并保留现有 session，不得误路由或写脏数据。

### 2.3 建议 SLO

- webhook 入队成功率 ≥ 99.95%，入队接口 p95 ≤ 200 ms。
- 在目标峰值 `C_peak` 下，业务回复成功率 ≥ 99.5%，端到端 p95 ≤ 8 s、p99 ≤ 20 s。
- 稳态压测容量必须 ≥ 预测峰值的 2 倍；在确定业务预测前，不允许用固定低 QPS 宣称通过。
- 队列等待时间 p95 ≤ 2 s；持续 5 分钟超过阈值触发告警和扩容。
- 同一用户乱序提交数为 0；锁丢失导致的并发 session 写数为 0。
- LLM JSON 解析失败率 ≤ 0.5%；v2 → legacy fallback 率 ≤ 1%。
- 搜索有 SQL 候选时，即便 reranker 超时/5xx/解析失败，返回业务结果的比例 ≥ 99.9%。
- 上传草稿非显式丢失率为 0。
- 核心支持矩阵离线语料 route/frame 准确率 ≥ 98%；高风险动作误执行率 ≤ 0.1%。

## 3. 初始基线代码审计

本节记录基线 `edd5183` 的问题，用于说明改造来源；其中缺口已按 §8 状态更新，不代表实施后现状。

### 3.1 文本发布岗位/简历

已具备：

- 岗位和简历有独立必填字段集合，入库前服务端再次检查。
- 多轮原文与结构化字段合并，入库前执行内容审核。
- 草稿有独立 TTL，成功、取消和过期均有清理路径。
- 数值、城市、工种、列表字段有后端归一化和范围校验。

缺口：

- 字段模型是闭集，未知招聘字段会被丢弃或仅作为 display 字段保留。
- legacy 补字段的规则兜底只覆盖人数、薪资、有限城市和关键词；未知口语高度依赖 LLM。
- `failed_patch_rounds >= 2` 直接调用 `clear_pending_upload()`，违反“未显式取消不丢草稿”的生产不变量。
- 失败回复缺少“已识别字段、仍缺字段、可用操作”的结构化恢复信息。

### 3.2 多轮补字段、取消、冲突切换

已具备：

- `active_flow` 是路由 source of truth，并有旧 session 自修复。
- upload collecting、upload conflict、search active、idle 相互隔离。
- 搜索 awaiting、上传 awaiting、放宽确认使用不同 session 字段，避免交叉污染。
- v2 reducer 与 applier 分离，角色权限、字段白名单、merge policy 均由后端裁决。

缺口：

- `dialogue_v2_mode` 默认 off，生产默认仍使用 legacy IntentResult。
- legacy upload conflict 依赖有限中文词表；v2 解析失败后仍回到该边界。
- `conflict_followup_rounds >= 2` 会清空草稿；更安全的行为应是放弃新意图、恢复原草稿。
- 草稿恢复提示没有携带完整状态摘要，用户难以自助回到正确路径。
- 目前没有状态机属性测试证明任意消息序列下不出现非法状态组合。

### 3.3 搜索、推荐、翻页、自动放宽

已具备：

- SQL 先做生命周期与硬条件过滤，最多读取配置化候选数。
- reranker 后保存候选快照，`show_more` 会重新校验记录有效性并避免重复。
- 快照保存 `effective_criteria`，自动放宽后的翻页可使用实际条件。
- 放宽步骤为固定白名单，并保留城市/工种守卫；二阶段有递归深度保护。
- 匹配依据使用确定性安全字段，不让 LLM生成解释。
- 推荐体验、软偏好排序、理由和 notice 均有独立 kill switch/rollout。

缺口：

- reranker 的 parse error 会降级，但 timeout、HTTP error 和未知异常仍向上抛，最终返回系统繁忙。
- post-search legacy stub 把 `accepted_slots_delta` 置空，无法可靠识别本轮新增硬条件。
- 放宽确认的 accept/reject 主要依赖 v2 `respond_relaxation_offer`；配置为 post-search on、v2 off 时缺少完整闭环。
- 工种同义词表与大类固定在代码中；未知工种保留原值后执行精确 SQL，容易产生假性零召回。
- 软偏好权重是业务直觉初值，尚无生产 shadow 数据证明排序收益。

### 3.4 未知表达与复杂组合意图

已具备：

- v2 DTO 支持多槽、多值、merge hint、clarification、conflict action 和低置信度处理。
- JSON 解析失败可退回 legacy；未知字段会被 schema 丢弃。
- 显式斜杠命令优先于模型输出。

缺口：

- golden runner 大量使用预置 parse/mock，证明 reducer 正确，不证明真实模型对未知表达的理解能力。
- 缺少匿名真实语料回放集、错别字/否定/反悔/混合表达 adversarial 集和按版本对比报表。
- 顺序意图（如“先苏州，不行再北京”）和指代解析（如“第二个怎么联系”）没有后端 DTO/状态契约。
- 没有 provider 级 circuit breaker；外部模型抖动时每条消息都可能支付完整重试时延。
- intent 超时会返回通用系统繁忙，缺少保留当前状态的领域化恢复回复。

### 3.5 高并发生产运行

已具备：

- webhook 异步入 Redis，Worker 消费，接口与慢 LLM 解耦。
- 同一用户使用 Redis 分布式锁，不同用户可通过多 Worker 横向扩展。
- 入站去重、失败重试、死信、队列积压监控和 Worker heartbeat 已存在。

缺口：

- `LOCK_TTL=30`，锁没有续租；一次 LLM 最多重试一次且单次 timeout 30 秒，完整消息还可能继续调用 reranker，锁可能处理中失效。
- 单 Worker 主循环严格串行；持续入站时 send-retry 主要在空闲分支处理，可能被长期推迟。
- 锁竞争失败后消息重新入队，多个同用户消息在多 Worker 下缺少显式 sequence/版本 CAS 保护。
- 缺少 queue wait time、message processing duration、lock lost/renew failure、降级率等生产指标。
- 现有压测证明了横向扩展趋势，但尚未以业务预测峰值的 2 倍完成持续与故障注入测试。

## 4. 分阶段改造方案

### P0：评测基线与生产契约

交付物：

- 建立 `conversation_eval` 语料格式，字段至少包括 role、初始 session、turns、期望 route/frame、槽位、状态转移、回复语义和禁止副作用。
- 建立三套数据：核心功能集、未知表达/adversarial 集、历史匿名回放集。
- 同一语料支持 mock reducer 测试和真实 provider 重复评测；输出准确率、fallback、clarify、误执行、延迟和 token。
- 增加状态机 invariant checker 和基于 Hypothesis 的消息序列属性测试。
- 固化本文件 §2 的指标与目标容量 `C_peak`。

验收：

- 至少 500 条单轮/多轮样本，三角色和四 frame 均有覆盖。
- 每个高风险状态转移至少 20 个正例、20 个反例。
- 真实模型连续 5 次回放有版本化报告，禁止只报告单次通过率。

### P1：状态安全与同用户一致性

代码改造：

- Redis user lock 增加持有期自动续租；续租失败时停止提交 session/DB 结果并进入可恢复重试。
- 为入站事件增加/复用单调顺序或 session version；保存 session 时执行 owner/version 校验，防止过期 Worker 覆盖新状态。
- 上传补字段连续未识别不再清草稿：重置失败计数、保留草稿并输出恢复卡片式文本。
- 上传冲突连续未确认时放弃 pending interruption、恢复原草稿，不清草稿。
- 所有取消/过期/成功清理路径增加结构化 `draft_cleared` reason 日志。

验收：

- 故意让单条消息处理超过 2 倍锁 TTL，第二 Worker 不能并发提交。
- 注入锁续租失败，旧 Worker 不得覆盖新 session。
- 任意非 cancel/expiry/success 消息序列都不能让 pending draft 从有变无。
- 旧 Redis session 可反序列化并继续完成发布。

### P2：LLM 降级与未知表达安全

代码改造：

- reranker 对 timeout/HTTP/parse/未知异常统一降级到确定性 SQL 顺序，日志区分原因。
- intent/provider 增加短周期 circuit breaker、并发上限和快速失败；打开时保留 session 并返回领域化可重试提示。
- 在 `pending_relaxation` 上下文增加精确闭集 accept/reject 解析，使 v2 off/fallback 时仍能完成确认。
- 为 cancel/reset/proceed/resume 只保留“系统明确给出的选项 + 精确匹配”兜底，禁止开放式 substring 扩张。
- 未知表达累计达到阈值时进入 clarification/recovery，不执行破坏性动作。

验收：

- 注入模型 timeout、429、500、非法 JSON：已有 SQL 候选仍能返回；session 保持合法。
- circuit breaker 打开时无重试风暴，恢复后可半开探测。
- 未知表达和否定表达不能误触取消、清空、自动放宽或方向切换。

### P3：搜索语义与复杂组合能力

代码改造：

- post-search 接收与 provider 无关的 `turn_asserted_fields`，legacy/v2 都能判断本轮新声明条件。
- 放宽确认、自动放宽、show_more 统一使用同一个 SearchContext/criteria provenance。
- 把工种大类、同义词和父子关系迁到版本化 ontology/config；未知值先映射/澄清，不直接拿任意原文做精确大类过滤。
- 引入受限 `DialoguePlan(actions<=2)`：后端校验角色、顺序和冲突；一次只执行一个有副作用动作，第二动作持久化为 pending action。
- 增加最近展示锚点；只对序号/唯一候选自动解析，歧义指代必须澄清。

验收：

- 用户本轮声明薪资/城市/工种时，任何放宽都先确认。
- legacy、v2、v2 fallback 三条路径对相同语义得到等价 SearchContext。
- ontology 未知值不会形成无提示的假性零召回。
- 两动作计划可恢复、可取消、不可跨角色越权；三动作以上明确澄清。

### P4：容量、背压与运行稳定性

代码改造：

- 将 inbound、send-retry、rate-limit notify 拆为独立消费职责或保证按时间片公平处理。
- 增加按 provider/DB 的并发 semaphore、队列长度与 queue-age 双重背压。
- 记录并告警：queue wait、process duration、LLM duration、lock wait/renew/lost、session version conflict、fallback、dead-letter。
- 定义 Worker 数量与 `C_peak` 的容量公式，给出扩容和降级 runbook。
- 负载过高时优先关闭软偏好 rerank/shadow，再限制低价值请求；不得牺牲取消、发布状态保存和基础搜索。

验收：

- 2×`C_peak` 持续 4 小时，满足 §2.3 SLO，无队列持续增长、无 session 乱序。
- 5×`C_peak` 突发 5 分钟后可在规定恢复时间内清空积压。
- 注入 20% LLM 超时、Redis 短断、MySQL 慢查询，核心状态不丢，降级符合预期。
- 扩缩 Worker 期间同一用户顺序不变。

### P5：灰度与生产验收

- v2 先 shadow ≥ 7 天，回灌差异 case；再按 5% → 25% → 50% → 100% 推进。
- post-search、推荐解释和软偏好分别独立灰度；不得把多个未知变量同时放量。
- 每一级至少观察一个完整业务周期；任一关键指标回退 ≥ 5% 自动/人工回滚。
- 100% 后稳定 ≥ 14 天，且完成 fallback case 人工抽检，才标记“生产验收完成”。
- 软偏好权重必须基于真实 shadow/接受行为重新校准；未经校准不得全量启用。

## 5. 测试矩阵

| 维度 | 必测内容 |
|---|---|
| 角色 | worker、factory、broker 双方向 |
| 发布 | 一轮完整、多轮乱序补字段、修改已填字段、未知字段、取消、过期、审核失败、DB 重试 |
| 冲突 | 发布中搜索、发布另一实体、继续原草稿、执行新动作、拒绝、未知回答、模型失败 |
| 搜索 | 首搜、follow-up 替换/追加/删除、0/1/2/3+ 召回、排序降级、权限脱敏 |
| 翻页 | 多页、记录过期/删除、快照过期、反复“更多”、放宽后翻页 |
| 放宽 | 自动、需确认、拒绝、确认过期、二次仍 0、禁止递归、legacy/v2/fallback 等价 |
| 语言 | 口语、省略、倒装、错别字、否定、反悔、多值、歧义、无关闲聊、prompt injection |
| 并发 | 同用户乱序风险、不同用户吞吐、锁续租、Worker 扩缩、重试公平性、积压恢复 |
| 故障 | LLM timeout/429/5xx/坏 JSON、Redis 短断、MySQL 慢/断、发送失败 |

## 6. 实施顺序与提交边界

建议保持小提交：

1. `test(conversation): add production invariants and eval corpus`
2. `fix(worker): renew per-user locks and fence stale writers`
3. `fix(upload): preserve drafts on unrecognized followups`
4. `fix(search): degrade safely when reranker is unavailable`
5. `fix(dialogue): close legacy relaxation confirmation loop`
6. `feat(search): track provider-independent asserted fields`
7. `feat(dialogue): add bounded action plans and reference anchors`
8. `feat(ops): add backpressure metrics and fair queue consumers`
9. `test(load): add sustained, burst, and chaos acceptance gates`

每个提交必须包含对应失败测试、向后兼容测试、结构化日志断言和回滚说明。禁止在同一提交同时修改语言策略、搜索召回和并发模型。

## 7. 初始实施建议（已执行）

实际按以下顺序完成：P1 锁续租与草稿保留 → P2 reranker 降级与确认闭环 → P0 真实语料评测框架 → P3 复杂表达 → P4 短时容量验证。长期容量与生产灰度仍按 §9 执行。

## 8. 实施结果与验证证据

### 8.1 已实施

- 状态安全：上传补字段/冲突未知回答不再隐式清草稿；所有清理路径带原因；两动作计划可查看、消费、取消且三动作以上安全澄清。
- 并发一致性：Redis 锁自动续租、owner fencing、session version CAS、同用户按 `wecom_inbound_event.id` 排序；恢复扫描使用数据库时钟、`SKIP LOCKED` 和先提交 claim 再入 Redis。业务事务把 session save/delete 意图写为 `session_pending`，提交后才 CAS 应用 Redis；失败由持久扫描幂等恢复，恢复完成前阻塞同用户后续事件，避免数据库已提交后重跑发布/搜索。
- 搜索降级：reranker timeout、HTTP、解析和未知异常统一回退确定性 SQL 顺序；队列积压时主动关闭低价值排序。
- 语言边界：v0.6 prompt、动态工种 ontology、字典支持的显式城市/工种原文锚点、broker 明确主客体方向锚点；legacy/v2/fallback 均以角色和明确主客体为后端裁决，provider 把明确换向误标为 follow-up/modify-search 时不会沿用旧方向。
- 持久化投递：业务数据、conversation log、回复 outbox 和 inbound done 原子提交；stale sending 可恢复；同一用户较晚回复不能越过较早未发送回复。
- 运维：结构化日志和用户标识哈希、DB/Redis 有界超时、queue age/process latency/outbox/session-commit 健康告警、outbox TTL/用户删除清理、生产 compose 和 nginx 上游健康改进。持久 session payload 在成功应用后立即置 SQL NULL，避免按 inbound TTL 保留临时搜索/草稿 PII。

数据库升级顺序：

1. `phase8_001_conversation_recovery_indexes.sql`
2. `phase8_002_inbound_event_microseconds.sql`
3. `phase8_003_wecom_outbound_outbox.sql`
4. `phase8_004_outbox_user_order_index.sql`
5. `phase8_005_durable_session_commit.sql`

### 8.2 2026-07-25 验证结果

| 验证 | 结果 |
|---|---|
| 完整单元套件 | 1281 passed |
| 真实服务烟测 | 13/13：DB 恢复、outbox crash 恢复、session commit crash 恢复、搜索/补槽/翻页、复杂意图、两动作、发布、草稿冲突、broker 切向、未知表达、事务出站 |
| 600 条真实模型合成广度集 | 599/600（99.83%），0 error，0 fallback，p95 1.255s；唯一失败已增加后端角色方向约束 |
| 唯一失败样本修复复测 | 连续 5/5 正确，0 error，0 fallback |
| 36 条人工标注集 × 3 | semantic 100%，stable 100%，0 error，0 fallback，p95 1.333s；3 个 exact diff 均为 `search_job`/`follow_up` 同语义族 |
| 16 Worker / 24 同时用户 | 24/24 成功；端到端 p95 4.253s；queue p95 1.288s；process p95 3.991s |
| 16 Worker / 60 同时用户 | 60/60 成功；0 failed；端到端 p95 5.071s；queue p95 4.025s；process p95 3.687s；同用户 3/3 有序。端到端达标但 queue p95 不达标 |
| 32 Worker / 60 同时用户 | 60/60 成功；0 failed；端到端 p95 4.022s；queue p95 1.976s；process p95 3.922s；同用户 3/3 有序。一次短突发满足建议 SLO，但不能替代持续压测 |
| 4 Worker / 24 同时用户（持久 session 协议后） | 24/24 成功；0 failed；端到端 p95 8.952s；queue p95 5.851s；process p95 3.696s；说明 4 Worker 不满足建议 p95，生产须按 `C_peak` 扩容 |
| 同用户并发顺序 | 3/3 按入站顺序提交，无乱序 |
| outbox stale claim | attempt 1→2 后 sent，观察窗口内只投递一次，业务事件未重跑 |
| session commit 故障注入 | MySQL 已提交、Redis key 缺失时自动恢复为 done；outbox 随后 sent；conversation log 未重跑；临时 session payload 已清空 |
| Redis 20 用户短断混沌 | Redis 暂停 4 秒；20/20 done，outbox 20/20 sent，无重复输入日志，会话 20/20，9.904s 内收敛 |
| MySQL 20 用户短断混沌 | MySQL 暂停 7 秒；20/20 done，outbox 20/20 sent，无重复输入日志，会话 20/20，15.437s 内收敛 |
| LLM 20% 混合故障 | 20 个会话中 4 个分别注入 timeout/429/500/坏 JSON；20/20 done，outbox 20/20 sent，会话合法且无残留 payload；服务端计数确认四类故障均命中 |

合成 600 条是覆盖三角色、十城市、十工种和四种模板的广度集，不替代匿名历史语料。测试中 `装配工`、个别 `仓储` 表达触发“未知 ontology 原值保留”警告；意图正确，但对应召回质量仍需真实词表持续扩充。

### 8.3 可重复执行命令

- `scripts/run_conversation_smoke_compose.sh`
- `scripts/run_conversation_load_compose.sh`
- `scripts/run_conversation_replay_compose.sh`
- `scripts/run_conversation_chaos_compose.sh`
- `scripts/run_conversation_llm_chaos_compose.sh`
- `scripts/apply_sql_migration_compose.sh <migration>`

所有脚本使用唯一测试用户并按用户边界清理；outbox、conversation log、event、job/resume 和 Redis session 均在清理范围内。

## 9. 正式生产仍需完成的门槛

以下不是代码单测可以替代的事项，因此当前结论是“可开始预生产和受控灰度”，不是“可直接 100% 全量”：

1. 明确业务预测 `C_peak`，以至少 `2×C_peak` 持续 4 小时，并执行 `5×C_peak` 5 分钟突发；当前只证明 16 Worker 可承受 24 同时用户短突发。
2. 建立至少 500 条匿名历史人工标注语料并连续 5 次回放；当前 600 条为合成广度集，人工集只有 36 条。
3. v2 shadow 至少 7 天，随后 5%→25%→50%→100% 分级放量；100% 后稳定观察至少 14 天。
4. 监控阈值上线：queue p95≤2s、端到端 p95≤8s、fallback≤1%、outbox/session commit 最老 pending≤300s、dead letter=0。4 Worker / 24 用户实测 queue p95 5.851s，不能直接作为生产容量配置。
5. 接受并在客服/runbook 中记录企微无幂等键导致的极小概率重复回复；若业务要求严格 exactly-once，必须由消息提供方提供幂等键或查询确认能力。
6. Redis 与 MySQL 仍不是单一 ACID 存储，但代码已用 MySQL `session_pending` 提交意图、同用户顺序门禁、Redis CAS/fencing 和幂等扫描闭合提交窗口。本轮 Redis/MySQL 短断与 20% LLM 混合故障已通过；正式长稳态演练仍须覆盖 Redis 故障超过 session TTL、Redis CAS 成功后 DB 回写失败、MySQL 在 post-commit 查询期中断，并监控 `session_commit_pending_age`。

达到以上门槛后，才能把“文本发布、推荐和多轮对话足够稳定”从工程验证结论升级为正式生产验收结论。
