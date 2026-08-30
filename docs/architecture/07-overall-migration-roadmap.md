# 07 总体改造路线图

> 状态：实施路线与验收记录（S2/S3 工程闭环已完成；生产 rollout 和 S4 仍受门禁约束）
> 范围：招聘双向流程、二手物品试点，以及后续多渠道/检索基础设施演进。  
> 原则：适配器先行、只读先行、每阶段可观测/可回滚；本文不把规划能力当作已实现能力。

## 1. 最终目标与不变约束

JobBridge 的最终形态是“统一 Listing Flow + 领域 Profile + Skill 驱动语义理解 + 有界编排 + 确定性策略内核”。企微仍是首个主要入口；FastAPI、MySQL、Redis、独立 Worker、Qwen/豆包 Provider 和 Vue 管理后台继续作为演进基座。

最终用户动作统一为：

```text
发布信息 -> 补齐字段 -> 确认 -> 审核 -> 发布
搜索信息 -> 修改条件 -> 推荐卡片 -> 更多 -> 联系
```

必须长期成立的约束：

1. 发布不能绕过确认/审核。LLM、Skill、MCP 都不能直接把草稿改成 `published`。
2. 招聘首期 `jobs`/`resumes` 仍是事实源，通过 Listing Facade 暴露公共协议，不做招聘业务双写。
3. 有效企微消息先落现有 `wecom_inbound_event`；限流记录复用现有 status 闭集，不新增 `accepted`/`rate_limited` status。
4. 幂等键为 `turn_id + action_name`；执行凭据使用 `action_execution` 的 lease/fencing，业务写入、Session CAS 和成功标记同事务提交。
5. `domain_outbox_event` 携带单调 `aggregate_version`；删除使用 tombstone，旧事件不得复活已删除索引。
6. 联系方式由服务端重新鉴权，使用短期一次性 token、频控、撤销、审计和 PII 保护；手机号不进索引、Prompt、普通对话日志或 Outbox payload。
7. 新运行时必须有量化 SLO、灰度停止条件和 legacy fallback。回滚切换路由，不删除已提交业务数据。

## 1.1 对 README 与 01-06 目标的覆盖

| 来源目标 | 本路线图落点 | 交付边界 |
|---|---|---|
| README：FastAPI + MySQL + Redis + Worker + 企微 + Qwen/豆包 + Vue 后台 | 全阶段共同基座；S0 先固化接入/恢复/观测，S7 再扩展渠道 | 不在迁移中替换现有部署栈或 `/admin/*` API |
| 01：统一 Listing Flow、Profile、确定性权限/审核/事务 | S1 协议/Reducer，S2-S6 各领域 Facade/Profile，S4/S5 发布审核，S6 二手复用 | 模型只理解，服务端执行；不允许开放式 Agent 直接写库 |
| 02：公共 Listing、生命周期、ListingCard、搜索/推荐、联系/隐私 | S2 ListingCard/Search Facade，S3 Contact，S4-S6 生命周期和领域 Profile | 招聘首期不把旧 jobs/resumes 强迁到公共表 |
| 03：Dialogue v1、Skill Registry、Domain Manifest、有界编排、本地 MCP | S1 版本化协议和 Skill/Prompt 资产；S2-S6 以 Profile/allowlist 驱动；S7 才远程化 | 首期 MCP 为 in-process adapter；不让模型生成 SQL/URL/任意 Tool |
| 04：可靠消息、幂等、Outbox、观测、SLO、容量演进 | S0 可靠入站；各写阶段 Action lease/fencing + Outbox；全阶段停止条件；S7 按指标扩容 | 失败降级不能跳过权限、审核和幂等 |
| 05：适配器迁移、回放、灰度、后台兼容 | 每阶段 adapter/shadow/replay/rollback；后台仅增兼容字段 | 不一次性重写 Router 或后台，不清理 legacy 直到签字下线 |
| 06：开源项目选型治理 | S1/S7 采用 Rasa/CALM、MCP SDK 的局部思想/组件；LangGraph/Temporal 仅在跨天流程，Dify 仅实验，Botpress 不进核心 | 依赖版本、许可证和镜像来源在上线前单独审查 |

## 2. 当前基线：已存在与待补齐

| 能力 | 当前代码事实 | 目标差距 |
|---|---|---|
| 接入与处理 | [`webhook.py`](../../backend/app/api/webhook.py) 已做验签、解密、Redis 去重、限流、入队；[`worker.py`](../../backend/app/services/worker.py) 有 claim/lease、用户串行锁、Session commit、Outbox 投递和恢复循环 | 限流通知仍可能发生在入站落库前；需要统一“先落库、再限流”的 durable inbox 契约并补 `turn_id`/限流审计字段 |
| 路由与意图 | [`message_router.py`](../../backend/app/services/message_router.py) 仍是大路由；[`intent_service.py`](../../backend/app/services/intent_service.py) 同时保留 legacy 与 Dialogue v2，支持 `off/shadow/dual_read/primary` | 需要将 v1 协议、Session/Reducer 和 Action 边界收敛到可替换 Runtime，避免继续增加 Router 分支 |
| Session | [`schemas/conversation.py`](../../backend/app/schemas/conversation.py) 已有 `SessionState`、搜索条件、候选快照、awaiting、冲突和放宽字段；`conversation_service.py` 已有 TTL/CAS 辅助 | 需要版本化 schema、`profile`、legacy compatibility projection 和统一 reducer 状态机 |
| 搜索 | [`search_service.py`](../../backend/app/services/search_service.py) 已有 SQL 硬过滤、LLM rerank、快照 `show_more`、零结果放宽、推荐实验和 legacy fallback；`search_permission.py`/visibility policy 已参与过滤 | 需要 `listing.search` Facade、结构化 `ListingCard`、版本化快照/outbox 索引链路，并保持旧 SQL 行为可回退 |
| 上传/审核 | [`upload_service.py`](../../backend/app/services/upload_service.py) 已有草稿、字段校验、敏感词/LLM 审核、岗位/简历入库和图片附件生命周期 | 需要统一 Listing Flow 和 Action 幂等；不能把上传迁移误写成已完成的公共 Listing 发布 |
| 数据与事件 | `Job`/`Resume` 已有 `version`、审核/有效期字段；S2/S3 已落地 Action lease/fencing、结果引用、parse artifact、Contact delivery Outbox；入站/出站 status 闭集保持不变 | 全域 `aggregate_version`/`domain_outbox_event`/删除 tombstone 仍属于 S4-S7；Action/Contact 生产 on 观察窗口待完成 |
| 联系与隐私 | Contact Domain Service、一次性 grant/delivery、频控/撤销/审计、PII ciphertext 和回填脚本已落地；迁移 verify 为 `ready_for_freeze=true` | 生产 Contact on 灰度、旧明文列清理审批和长期观察窗口尚未完成；默认仍 feature-off |
| 管理后台 | Vue 3/Element Plus 后台已覆盖账号、岗位、简历、审核、配置、看板 | 只增加 profile/trace/version 等兼容字段，不改 `/admin/*` 路径和核心交互 |

## 3. 目标分层与阶段依赖

```text
S0 基线可靠性/观测
   ↓
S1 Dialogue v1 + Session/Reducer
   ↓
S2 求职搜索 v1（worker -> job） ──→ S3 联系方式/隐私
   ↓                                  ↓
   └──→ v1 后续 Action/Contact/灰度门禁 ──→ S4 岗位发布（factory/broker）
   ↓
S5 简历发布 + 双向招聘（job/resume）
   ↓
S6 二手物品试点（普通 user）
   ↓
S7 多渠道 / 远程 MCP / 大规模检索
```

S2 依赖 S0 的可回放基线和 S1 的协议/状态，但允许在 S1 未全量切换时通过 adapter 接入。S3 的联系入口可在 S2 输出 opaque request，但实际明文兑换必须在 S3 完成。S2 完成后，Action Execution 的生产接入、Contact/PII 闭环、灰度演练和 legacy 退出门禁按 [10 v1 后续 Action/Contact 实施方案](10-post-v1-action-contact-implementation-plan.md) 执行；该方案完成前不得启动 S4。S4/S5 依赖稳定的审核、Action 幂等和 outbox；S6 不得反向改写招聘事实源；S7 必须由容量和复用需求触发。

## 4. 分阶段路线图

### S0：基线可靠性与观测

**前置依赖**：无；以当前 FastAPI、MySQL、Redis、Worker 和企微 Webhook 为基线。

**目标与需求**

- 建立岗位发布、简历发布、worker 找岗位、factory/broker 找工人、上传冲突、放宽确认、`show_more`、权限/过期/脱敏和 LLM/Redis/企微故障的 golden replay 集。
- 有效消息先写 `wecom_inbound_event`；扩展 `turn_id`（唯一）、`rate_limit_decision`、规则版本和时间字段，保持 `received/processing/session_pending/done/failed/dead_letter` 闭集。
- Redis 队列故障时由 dispatcher 扫描 `status=received AND rate_limit_decision=accepted` 补投；限流消息写 `done + rate_limited` 并可审计，不默认重放。
- 为每轮写入 trace、模型/provider、skill/profile/schema 版本、工具状态、延迟、fallback 原因和 backlog 指标。

**边界与主要模块**

只修接入、恢复、日志和指标，不切换业务路由、不引入新搜索引擎。主要模块：`api/webhook.py`、`services/worker.py`、`models.py`、数据库迁移、`core/redis_client.py`、日志/指标组件、回放工具和告警配置。

**数据迁移、回滚与验收**

采用向后兼容列和状态值；旧 Worker 继续识别原 status。迁移失败只回退 schema 变更，不删除已有事件。验收：有效消息持久化成功率 ≥99.99%、消息丢失率为 0、入站最老未处理 <60 秒；Redis/数据库故障行为符合 04 文档；所有 golden case 可重放并能解释 legacy/v2 差异。

**风险与决策点**

- MySQL 写入延迟可能挤压 Webhook 线程池：决定连接池上限、超时和降级告警。
- 限流通知是否重试：默认非关键出站可丢弃，但必须记审计。
- 观测字段中的 userid/原文脱敏粒度需由安全负责人确认。

### S1：Dialogue 协议与 Session/Reducer

**前置依赖**：S0 的 durable inbound、turn_id、回放集和基础观测可用。

**目标与需求**

- 固化 `DialogueParseResult` v1 闭集：`start_search`、`modify_search`、`answer_missing_slot`、`show_more`、`start_upload`、`cancel`、`reset`、`resolve_conflict`、`respond_relaxation_offer`、`chitchat`；冲突和放宽字段保持互斥校验。
- 以 `ActorContext`、Profile Schema、Policy Reducer 和 Action allowlist 为确定性边界；LLM 只产出理解结果，不写 Session/DB、不判权限。
- Session 增加 `schema_version`、`version`、`profile` 和兼容投影；保留现有 awaiting、pending_relaxation、candidate snapshot 语义。
- 通过 `IntentResult -> Dialogue v1` 映射兼容现有 Provider；未知 schema/slot 进入 fallback，不猜测。

**边界与主要模块**

不迁移发布或搜索 SQL；不引入 LangGraph/Temporal。主要模块为 `llm/base.py`、`intent_service.py`、`dialogue_reducer.py`、`dialogue_applier.py`、`dialogue_compat.py`、`conversation_service.py`、`schemas/conversation.py` 和新增的 `conversation/runtime.py`/`policies.py` adapter。

**数据迁移、回滚与验收**

新 Session 版本可读旧 Redis JSON，写入 legacy projection；`off` 继续纯 legacy，`shadow/dual_read/primary` 按白名单/桶灰度。回滚关闭新 Runtime 分流即可，未完成 turn 由入站 lease 恢复。验收：协议 schema/Reducer 单测 100% 通过；冲突、放宽、取消、reset 和会话过期回放结果与基线一致；非法状态迁移为 0。

**风险与决策点**

- `message_router.py` 约 2300 行，决定先 adapter 化还是分段抽离；不得在迁移期继续加入领域分支。
- Session schema 版本与 Redis TTL 的兼容窗口由运维确认。

### S2：求职搜索首版（worker 搜索 job）

> 状态（2026-08-30）：S2 工程实现、代码审查、WSL 部署和 mock 页面端到端验证已完成；S3 的 Action/Contact 后续接入也已完成工程闭环。生产 rollout 仍保持 legacy/fallback 优先，详见 [09 Action Execution 审计](09-job-search-v1-action-execution-audit.md) 和 [10 后续实施方案](10-post-v1-action-contact-implementation-plan.md)。

**前置依赖**：S0 回放/可靠性基线；S1 的 v1 adapter、Session CAS 和 Reducer 可在灰度桶运行。

**目标与需求**

- 实现 `recruitment.job` Search Facade：结构化条件 -> 权限/状态/有效期硬过滤 -> 现有 Top-N/rerank -> 脱敏 `ListingCard`。
- 支持多轮条件修改/放宽、`show_more` 消费快照、低召回确认和旧行为 fallback；首期不做岗位发布、简历发布、向量库或远程 MCP。
- 按 worker 角色校验可见岗位，仅返回 `audit_status=passed`、未删除、未过期、未下架且允许该 actor 的数据。

**边界与主要模块**

主要使用 `search_service.py`、`search_permission.py`、`visibility_policy.py`、`schemas/search.py`、`schemas/conversation.py` 和新增 `listing/search.py`、`listing/render.py` Facade。`message_router.py` 只增加一处 adapter 路由；`upload_service.py` 不改业务。

**数据迁移、回滚与验收**

招聘表继续为事实源；可先 shadow read，不要求新旧双写。候选快照沿用 Redis 结构并增加算法/策略版本。按 worker 用户灰度，kill switch 直接回 legacy。普通搜索 P95 ≤5 秒、命令/更多 ≤1.5 秒、搜索无权限/过期泄露为 0；具体实施见 [08 首版详细实施方案](08-job-search-v1-implementation-plan.md)。v1 完成后的 Action Execution 生产接入、搜索/show_more/relaxation replay，以及 Contact/PII 后续实施，不在本阶段直接扩展，统一按 [10 v1 后续 Action/Contact 实施方案](10-post-v1-action-contact-implementation-plan.md) 推进。

**风险与决策点**

- LLM rerank 超时是否使用硬过滤顺序：默认硬过滤结果 + 固定模板。
- 新 `ListingCard` 与企微文本模板字段映射需产品确认，但不得暴露原始 phone。

### S3：联系方式与隐私

**前置依赖**：S2 的 Card/contact_request_id 输出、服务端 ActorContext 和审计链路稳定。

> 状态（2026-08-30）：Contact Domain Service、一次性 grant/delivery、频控、撤销、审计、PII 加密回填、Contact Outbox 投递和 C2 故障矩阵已完成实现与验证；Contact 默认仍为 `off`，生产 on 灰度、旧明文列清理审批和长期观察窗口待完成。

**详细实施方案**：本阶段的实现与验收记录统一见 [10 v1 后续 Action/Contact 实施方案](10-post-v1-action-contact-implementation-plan.md) 的 Workstream B/C。

**目标与需求**

- `ListingCard.contact_action` 只带不透明 `contact_request_id`；点击后由 Contact Domain Service 重查 Listing 事实源、版本、审核/有效期、actor 认证、角色、黑名单和频控。
- 通过后签发绑定 actor/listing/action/nonce 的一次性短期 opaque token（建议 TTL 60 秒），支持下架、封禁、策略变更即时撤销。
- 明文 phone 进入加密 Contact 存储；不进入索引、Prompt、Skill、普通日志、卡片或 Outbox；后台明文查看需二次授权和审计。

**边界与主要模块**

新增 `listing/contact.py`、PII 存储/密钥配置、频控和审计；搜索 Facade 只生成入口，不自行解密/展示。企业微信发送仍走 Outbox。

**数据迁移、回滚与验收**

历史 phone 采用分批加密回填，保留旧列只读过渡，完成抽样校验后再按保留策略清理；任何失败停止回填，不影响搜索。验收：跨用户/越权泄露为 0；每 actor+listing 每 10 分钟最多 3 次、每 actor 每日最多 30 次（值可配置）；token 兑换一次且过期/撤销立即失败；成功/失败均可审计。

**风险与决策点**

- 密钥托管、轮换和历史明文清理期限需安全/合规确认。
- 企微消息重复投递时的用户体验由 Outbox 去重和文案策略共同决定。

### S4：岗位发布

**前置依赖**：S0/S1 的 Action 幂等和状态机；S2 的 Listing Facade/卡片契约；S3 的 PII 边界。

**目标与需求**

- 将 factory/broker 发布接入公共草稿、补字段、确认、审核、发布、过期、下架和恢复 Flow；保留图片、冲突、TTL 和后台审核。
- 任何自动审核必须是服务端策略、带规则版本和审计的显式路径；不存在 `draft -> published` 旁路。
- 引入 Action 幂等、业务唯一约束、事务 Outbox；`aggregate_version` 在每个事实源更新事务中 CAS 递增。

**边界与主要模块**

主要模块为 `upload_service.py` adapter、`listing/publish.py`、`moderation`、`attachment`、`admin/jobs.py` 兼容层；不改变 jobs 表为公共 listing 表。

**数据迁移、回滚与验收**

给 `jobs` 增加/回填 `aggregate_version=1`；创建、更新、审核、过期、下架、删除均同事务写 `domain_outbox_event`。Indexer 失败可回退旧 SQL；新发布已提交数据由 legacy 可读。验收：确认/审核覆盖率 100%、重复发布率 0、审核状态与后台一致、Outbox 最终发送 ≥99.9%。

**风险与决策点**

- 历史脚本/后台是否绕过 Service：未统一收口前不得宣布索引一致性完成。
- 自动审核规则的允许范围由运营和合规共同签字。

### S5：简历发布与双向招聘

**前置依赖**：S4 岗位发布审核、aggregate_version/outbox 和联系策略已通过生产观察窗口。

**目标与需求**

- worker 发布简历，factory/broker 搜索 worker；复用岗位 Search Facade、ListingCard、联系策略和曝光审计。
- 建立 `recruitment.job` ↔ `recruitment.resume` 的 MatchingPolicy；支持“找岗位/找工人”方向隔离和 broker 显式切换。
- 简历敏感字段（年龄、电话等）按 actor/策略投影，不能因双向搜索绕过脱敏。

**边界与主要模块**

扩展 `listing/search.py`、`domains/recruitment/matching.py`、`upload_service.py` resume adapter、后台简历审核；不引入跨领域统一推荐模型。

**数据迁移、回滚与验收**

给 `resumes` 增加/回填 `aggregate_version`，同样写 outbox；先按方向 shadow/灰度。回滚切回原 `search_workers`/上传路由，保留已写入事实源。验收：四条招聘核心 Flow 回放无回归；方向串线、越权和敏感字段泄露为 0；匹配/联系转化和 P95 达到 SLO。

**风险与决策点**

- broker 双向搜索的角色/方向状态需与现有 `broker_direction` 兼容，避免旧 Session 被误解释。
- 何时启用更复杂匹配取决于行为数据，首期只用可解释硬过滤+有限重排。

### S6：二手物品试点

**前置依赖**：S1 公共 Dialogue/Flow、S3 联系与隐私、S4/S5 审核/删除事件契约已稳定；普通 user 角色已完成后台和审计接入。

**目标与需求**

- 新增普通 `user` 角色和 `secondhand.item` Profile；复用公共 Flow、审核、搜索、联系、审计和卡片。
- 新增 `listing` + `listing_detail_item` 事实表（与招聘旧表隔离），字段包括品类、价格、成色、城市、图片和交付方式。
- 普通 user 可发布/搜索/联系二手物品；worker/factory/broker 默认不获得二手能力，admin 只走后台。

**边界与主要模块**

新增 `domains/secondhand/*`、Profile/Skill/字典、item migration、后台视图；不在招聘 Router 增加二手分支，按 Profile Registry 注册。

**数据迁移、回滚与验收**

无招聘数据回填；新表和索引按 checkpoint 可暂停重跑。回滚停止二手新写入和对外读取，保留数据供后台核查，不破坏性删除。验收：招聘四条 Flow golden replay 不变；普通 user 权限矩阵生效；审核、删除 tombstone、联系和 PII 保护测试通过。

**风险与决策点**

- 普通 user 初始化、后台筛选和审计主键关联必须先于开放发布。
- 图片审核和存储成本是否达到独立队列阈值需按试点数据决定。

### S7：多渠道、远程 MCP 与大规模检索

**前置依赖**：至少一个跨渠道复用需求已确认，且 S2-S6 的权限、审核、幂等和 SLO 指标达到稳定门槛。

**目标与需求**

- 在存在客服工作台、小程序或外部 Agent 复用需求后，才把本地 MCP adapter 拆为 Streamable HTTP Server；Tool 契约和服务端授权不变。
- 当 MySQL SQL 硬过滤、P95、backlog、数据规模或召回指标达到阈值时，引入 OpenSearch/Elasticsearch、地理检索或向量召回；向量只扩大召回，不替代权限/状态硬过滤。
- 各渠道共享 `conversation/runtime`、Listing Facade、Contact Domain Service 和 outbox，渠道适配层不复制业务规则。

**边界与主要模块**

新增 `channel/*`、`mcp/server/*`、indexer、搜索双读适配器和容量配置；不以 LangGraph/Dify/Temporal 取代既有事务内核，除非出现跨天人工等待或交易履约需求。

**数据迁移、回滚与验收**

先双写/回放索引，再 shadow read，最后按 Profile/渠道灰度；索引不可用回退 SQL。远程 MCP 可单独撤销 endpoint/凭据，业务数据不回滚。验收：权限/审核/幂等契约跨渠道一致；索引延迟、P95、召回和成本达到经压测批准的阈值；任何渠道泄露立即停止。

**风险与决策点**

- 远程 MCP 的身份传播、租户隔离、速率限制和网络边界需安全评审。
- 向量库的 PII 脱敏、删除传播和 tombstone 保留窗口需在采购前定稿。

## 5. 通用数据迁移与回滚规则

### 5.1 事实源与事件

招聘继续以 `jobs`/`resumes` 为事实源；二手以 `listing`/`listing_detail_item` 为事实源。所有写事务更新事实源、递增 `aggregate_version`，并在同一事务写入 `domain_outbox_event`。事件 payload 仅是脱敏索引提示；Indexer 必须回源校验状态和版本。`deleted` 事件为 tombstone，旧版本事件一律丢弃，防止删除后复活。

### 5.2 回填与切换

1. 按主键分页回填，保存 checkpoint、批次校验和可暂停状态。
2. 对数量、状态、抽样字段、权限过滤和 PII 排除做校验。
3. 先 shadow compare 旧 SQL 与新 Facade/索引，再按用户、角色、Profile 灰度。
4. 切换开关必须独立于业务配置，记录版本、操作者、原因和影响范围。
5. 回滚只停止新 Runtime 分流；允许已开始 turn 在租约窗口内完成，超时由入站事件和 Action lease 恢复；已提交 Outbox 继续发送。

### 5.3 统一灰度停止条件

连续 15 分钟或累计 1000 条新 Runtime 请求内出现任一情况，停止扩大流量并切回 legacy：

- 任意权限、联系方式或跨用户数据泄露；
- 任意重复发布/删除、绕过确认/审核或非法状态迁移；
- 普通搜索 P95 > 8 秒，命令/`show_more` P95 > 2.5 秒；
- LLM fallback >10%、Tool 5xx >2%；
- 入站最老消息 >120 秒、积压持续增长；
- Outbox 失败率 >1% 或死信 >20 条/小时。

## 6. 路线图决策门

每阶段进入下一阶段前，项目负责人、后端、AI、测试、运维和安全共同确认：

- golden replay 与契约测试通过，现状/目标差异已登记；
- SLO、成本预算、告警和停止条件已在 staging 演练；
- 数据迁移 checkpoint、回滚开关和操作手册可用；
- 管理后台、legacy adapter 和新 Runtime 的版本兼容矩阵已更新；
- 未决风险有明确 owner 和期限，否则保持 shadow/小流量，不扩大生产。
