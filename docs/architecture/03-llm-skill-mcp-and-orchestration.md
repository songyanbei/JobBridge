# 03 LLM、Skill、MCP 与有界编排

> 读者：AI、后端和安全研发。
> 目标：规定模型能做什么、不能做什么，以及如何通过配置支持多领域。

## 1. 核心原则

```text
概率理解，确定性执行
```

LLM 处理自然语言的不确定性；领域服务、权限、审核、数据库写入和消息发送由确定性代码完成。

## 2. LLM 职责

允许模型处理：

- 识别领域和用户操作；
- 从口语中抽取标题、正文、地区和领域属性；
- 判断字段新增、替换、追加或删除；
- 识别确认、取消、更多、修改、放宽和闲聊；
- 对已确定的推荐结果生成解释。

禁止模型处理：

- 角色和权限判定；
- 直接修改 Session、Redis 或数据库；
- 生成 SQL、代码、URL 或任意 HTTP 请求；
- 改变审核、封禁、删除、下架状态；
- 决定联系方式是否展示；
- 从数据库全集自行筛选候选；
- 发现并调用未授权 Tool。

## 3. 结构化语义协议

首期协议沿用当前 `backend/app/llm/base.py` 的 Dialogue v1 闭集，不能在重构中丢失冲突和放宽确认分支：

```python
class DialogueParseResult(BaseModel):
    schema_version: Literal["dialogue.v1"] = "dialogue.v1"
    dialogue_act: Literal[
        "start_search",
        "modify_search",
        "answer_missing_slot",
        "show_more",
        "start_upload",
        "cancel",
        "reset",
        "resolve_conflict",
        "respond_relaxation_offer",
        "chitchat",
    ]
    frame_hint: Literal[
        "job_search",
        "candidate_search",
        "job_upload",
        "resume_upload",
        "none",
    ] = "none"
    slots_delta: dict[str, object] = Field(default_factory=dict)
    merge_hint: dict[str, Literal["replace", "add", "remove", "unknown"]] = Field(
        default_factory=dict
    )
    needs_clarification: bool = False
    confidence: float = Field(ge=0, le=1)
    conflict_action: Literal[
        "cancel_draft", "resume_pending_upload", "proceed_with_new"
    ] | None = None
    relaxation_response: Literal["accept", "reject"] | None = None
```

字段约束：`conflict_action` 只有 `dialogue_act=resolve_conflict` 时允许出现；`relaxation_response` 只有 `dialogue_act=respond_relaxation_offer` 时允许出现。`slots_delta` 和 `merge_hint` 只是候选理解结果，必须经过当前 Session、Profile Schema、字典、角色和 Policy Reducer。

### 3.1 旧版兼容映射

旧 `IntentResult` 继续作为输入兼容层，不能直接进入 Runtime：

| 旧 IntentResult | Dialogue v1 |
|---|---|
| `search_job` | `dialogue_act=start_search`, `frame_hint=job_search` |
| `search_worker` | `dialogue_act=start_search`, `frame_hint=candidate_search` |
| `upload_job` | `dialogue_act=start_upload`, `frame_hint=job_upload` |
| `upload_resume` | `dialogue_act=start_upload`, `frame_hint=resume_upload` |
| `upload_and_search` | `start_upload`，由服务端 Action 记录后续搜索，不由模型拆成新协议 |
| `follow_up` | 根据当前 awaiting 状态映射为 `answer_missing_slot` 或 `modify_search` |
| `show_more` | `show_more` |
| `command: cancel/reset` | `cancel` / `reset`，显式命令优先跳过 LLM |
| `upload_conflict` | `resolve_conflict` + `conflict_action` |
| `relaxation answer` | `respond_relaxation_offer` + `relaxation_response` |
| `chitchat` | `chitchat` |

缺失或非法字段进入 schema fallback，不静默丢弃；解析失败时使用规则/legacy parser。未来新增 `contact`、`report` 等动作必须发布 `dialogue.v2`，并提供 v1 到 v2 的双向兼容映射和回放集。

### 3.2 文档协议与当前代码的兼容边界

当前代码中的 `backend/app/llm/base.py::DialogueParseResult` 尚未包含 `schema_version`；文档中的 `schema_version` 是目标内部协议字段，不要求旧 Provider 在第一天返回。兼容规则固定如下：

1. Provider 返回没有 `schema_version` 的 JSON 时，兼容适配器在完成 v1 闭集校验后补成 `dialogue.v1`；该补值只存在于 Runtime 内部，不回写旧 Provider。
2. Provider 明确返回未知 `schema_version`、未知 `dialogue_act`、未知 `frame_hint` 或非法的冲突/放宽枚举时，视为解析失败，保留脱敏后的 `raw_response`，走规则/legacy parser，不猜测映射。
3. 顶层语义字段采用 strict validation；`slots_delta` 的 key 必须由当前 Profile Schema 识别。未知 slot 不进入 Session 或 Tool，记录 `unknown_slot` 并生成澄清/legacy fallback。
4. 解析失败、schema 校验失败和 fallback 原因都写入 conversation/tool trace；原始响应只用于受控调试，按现有对话日志 TTL 和隐私策略保存。
5. 旧 `IntentResult` 只允许通过上一节映射表进入 Dialogue v1，不能将旧字段直接拼接成 Action；实现阶段先保留 `DialogueParseResult` 旧 DTO，再增加内部版本化 wrapper，避免一次改动破坏现有 Provider。

## 4. Skill 设计

### 4.1 Skill 的定位

Skill 是版本化的语言资产，不是任意可执行程序。每个 Skill 包含：

- 当前 Profile 允许的 acts；
- 字段自然语言表达和同义词；
- 城市、工种、品类别名；
- 补字段、纠正、确认、拒绝和打断样例；
- 追问顺序和固定回复模板；
- Prompt Injection 防护约束；
- golden examples、版本号和变更说明。

Skill 使用 Markdown + YAML frontmatter，由 `SkillRegistry` 负责加载、缓存、灰度和回滚。

### 4.2 Prompt 组装

每轮只注入必要上下文：

```text
系统约束
  + 当前 Profile 摘要
  + 当前 Flow 和允许 acts
  + 已填/待填字段摘要
  + 最近少量对话摘要
  + 当前 Skill 片段
  + 输出 JSON Schema
```

不能把全部 Skill、完整历史和候选全集塞给模型。

## 5. Domain Manifest

标准领域通过 Manifest 声明字段和动作，以减少重复代码：

```yaml
domain: secondhand
entity: item
operations: [publish, search, show_more, contact]
fields:
  category: {type: taxonomy, required: true}
  title: {type: text, required: true}
  body: {type: text, required: true}
  price: {type: money, required: true, min: 0}
  city: {type: city, required: true}
search:
  hard_filters: [category, price, city]
  ranking: [text_relevance, distance, freshness]
policies:
  moderation: secondhand_default
  contact_visibility: authenticated_only
```

Manifest 可以驱动字段收集、追问、校验引用、搜索字段和 Skill 上下文，但不能生成任意 SQL，也不能替代特殊业务插件。

## 6. 有界模型编排

### 6.1 适用范围

低风险场景可以让模型提出有限短计划：组合搜索、筛选、排序、推荐解释和跨领域浏览。发布、删除、审核、封禁、付款和公开联系方式必须进入固定确认流程。

### 6.2 Plan Compiler

```text
LLM ActionProposal/Plan JSON
  -> Pydantic Schema Validator
  -> Profile Registry 校验
  -> AuthorizationPolicy 校验
  -> Action Allowlist 校验
  -> Plan Compiler
  -> Idempotent Executor
```

模型只能引用预注册动作和字段，不能执行 SQL、Python、URL 或未知 Tool。

### 6.3 操作风险分级

| 风险 | 示例 | 执行方式 |
|---|---|---|
| 低 | 识别领域、搜索、筛选、推荐解释 | 可以有限编排 |
| 中 | 创建草稿、补字段、挂附件、发起联系 | 模型提议，平台校验后执行 |
| 高 | 发布、删除、审核、封禁、付款、公开联系方式 | 固定 Flow + 确认 + 幂等事务 |

## 7. MCP 设计

### 7.1 部署策略

第一阶段使用官方 MCP Python SDK 的本地 adapter：

```text
Agent Runtime -> in-process MCP adapter -> existing Domain Service
```

需要客服工作台、小程序或外部 Agent 复用时，再拆成 Streamable HTTP MCP Server。部署方式变化不改变 Tool 业务契约。

### 7.2 Tool Namespace

```text
platform.session.get_context
platform.session.apply_command
platform.moderation.check
platform.attachment.attach
platform.notification.enqueue

listing.publish_draft
listing.confirm_publish
listing.search
listing.show_more
listing.contact
listing.report

recruitment.match_job_resume
```

每一轮只向模型暴露当前 Profile 的 Tool 白名单。

### 7.3 Tool 安全契约

服务端注入以下字段，模型不能伪造：

```json
{
  "actor_context": "server-generated",
  "conversation_id": "...",
  "session_version": 12,
  "idempotency_key": "...",
  "trace_id": "..."
}
```

Tool 返回稳定 JSON、错误码和下一步建议。禁止提供 `database.query`、`execute_sql`、`do_everything` 或接受模型 `userid/role` 的 Tool。

## 8. 失败和降级

```text
LLM 正常 -> 结构化解析 + Listing Runtime
LLM 超时/解析失败 -> 规则/legacy parser
重排不可用 -> 硬过滤结果 + 固定模板
MCP adapter 异常 -> Domain Service fallback
企微出站失败 -> Outbox retry
```

降级不能跳过权限、审核和幂等检查。
