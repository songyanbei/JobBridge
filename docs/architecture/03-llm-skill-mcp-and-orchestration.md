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

建议将现有 `DialogueParseResult` 统一为稳定版本：

```python
class DialogueParseResult(BaseModel):
    schema_version: Literal["dialogue.v1"] = "dialogue.v1"
    act: Literal[
        "start_publish", "answer_missing_slot", "modify_listing",
        "start_search", "modify_search", "show_more", "confirm",
        "cancel", "reset", "contact", "report", "chitchat"
    ]
    profile_hint: str | None = None
    slots_delta: dict[str, object] = Field(default_factory=dict)
    slot_ops: dict[str, Literal["add", "replace", "remove", "unknown"]] = Field(
        default_factory=dict
    )
    confidence: float = Field(ge=0, le=1)
    needs_clarification: bool = False
```

`slots_delta` 只是候选理解结果。服务端必须根据当前 Session、Profile Schema、字典、角色和 Policy 重新归一化。

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
