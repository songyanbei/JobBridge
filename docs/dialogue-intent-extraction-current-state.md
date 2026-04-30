# 多轮会话意图识别与信息抽取：现状、决策与实施指南

日期：2026-04-28

## 0. 当前基线

本文档以最新提交 `fdeb18d` 作为当前基线：

```text
fix(backend): Bug 4/5/6 — broker 找工人 0 命中链路三连修
```

该提交已经覆盖一批局部修复：

- 搜索字段归一：搜索 / follow_up 场景中，服务端把 `expected_cities` / `expected_job_categories` 重映射到 `city` / `job_category`。
- 城市规范化：`北京` / `苏州` 等短名会通过 `dict_city` 和常见城市兜底归一到 `北京市` / `苏州市`。
- follow_up 条件替换：不再让 LLM 输出 `add/update` op，而是要求 follow_up 输出完整 criteria 快照，后端通过 `replace_criteria` 物化，避免 “换成苏州” 被误合并成 `["北京", "苏州"]`。
- prompt 约束增强：明确搜索字段、follow_up 完整快照、broker 找工人和城市替换/追加 few-shot。
- 文档基线：新增 [keyword-rules-audit.md](keyword-rules-audit.md) 和本文档。

因此，本文不是“从零修 bug”，而是在 `fdeb18d` 上继续把局部兜底演进成通用多轮对话理解层。

## 1. 背景

一个典型 worker 对话：

```text
用户：西安，想找个饭店的服务员的工作
系统：信息还不够完整，请补充：
- 月薪下限
- 计薪方式
- 招聘人数

用户：2500
系统：为您找到 1 个匹配岗位：
① 华东电子有限公司 | 餐饮
   3000元/月
   西安市

用户：北京有吗
系统：仍返回西安市岗位
```

这段里至少有三类问题，不能混为一谈：

1. 第一轮被追问“计薪方式 / 招聘人数”的根因，不是单纯 `missing_fields` 没重算，而是 LLM 把 worker 的“想找工作”整体误判成了 `upload_job`。`pay_type` / `headcount` 本来就是岗位上传必填，不是搜索必填。因此优先需要 intent/frame 纠偏：worker 角色 + “找/想找/求职/工作”等信号，应强制落到 `job_search`。
2. 第二轮 `2500` 是上一轮追问或当前搜索条件的薪资补充。若搜索流程没有物化 `awaiting_field`，后端无法稳定判断这是 `salary_floor_monthly`。
3. 第三轮 `北京有吗` 是高歧义表达：可能是“替换成北京”，也可能是“北京也一起看看”。不能在文档里把它不证自明地写死为 replace；需要由 reducer 根据上下文、语义信号和置信度裁决，必要时反问澄清。

## 2. 当前链路现状

### 2.1 主流程

当前文本消息主链路在 [message_router.py](../backend/app/services/message_router.py)：

```text
process
  -> _handle_text
    -> load/create SessionState
    -> record_history(user)
    -> classify_intent(...)
    -> 按 active_flow 路由
      -> idle/search_active/upload_collecting/upload_conflict
    -> _handle_search / _handle_follow_up / _handle_upload / ...
    -> record_history(assistant)
    -> save_session
```

`classify_intent` 当前会接收：

- 当前文本 `text`
- 用户角色 `role`
- 最近对话历史 `history`
- 当前搜索条件 `current_criteria=session.search_criteria`
- `session_hint`，但 provider 当前只记录其 key，不真正拼入 prompt

### 2.2 当前 LLM 输出结构

当前结果定义在 [base.py](../backend/app/llm/base.py)：

```json
{
  "intent": "search_job | search_worker | follow_up | upload_job | upload_resume | ...",
  "structured_data": {},
  "criteria_patch": [],
  "missing_fields": [],
  "confidence": 0.0
}
```

问题是：`intent` 同时承担业务目标和对话行为，`structured_data` 有时表示“本轮抽到的字段”，有时表示“完整 criteria 快照”，`criteria_patch` 又曾经承担过替换/追加语义。这使后端很难稳定判断本轮应该继承、覆盖、追加、追问还是切换流程。

### 2.3 现有局部能力清单

| 能力 | 当前实现 | 备注 |
|---|---|---|
| 后端真实流程状态 | `SessionState.active_flow = idle / search_active / upload_collecting / upload_conflict` | 已经是一个后端状态机 |
| 搜索条件累积 | `session.search_criteria` | `_handle_search` 合并，`_handle_follow_up` 可替换 |
| 上传草稿状态 | `pending_upload` / `pending_upload_intent` / `awaiting_field` | `awaiting_field` 目前主要服务上传补字段 |
| 搜索字段归一 | `_SEARCH_FIELD_REMAP` | fdeb18d 已覆盖搜索字段错位 |
| 城市归一 | `_normalize_city_value` | fdeb18d 已覆盖短名/规范名问题 |
| follow_up 全量替换 | `conversation_service.replace_criteria` | fdeb18d 避免 `add/update` op 歧义 |
| 关键词审计 | [keyword-rules-audit.md](keyword-rules-audit.md) | 其中 HIGH 项应逐步交给 dialogue_act |

这些能力说明底座已经在，但还缺一个统一的“语言理解结果 → 后端状态裁决”的中间层。

## 3. 关键设计决策

### 3.1 active_flow 与 frame 的关系

必须明确主从关系：

- `active_flow` 是后端真实状态，是 source of truth。
- `frame` 是本轮语言理解得到的候选业务对象，不直接写 session。
- reducer 根据 `active_flow + frame_hint + dialogue_act + slots_delta + session` 裁决下一步。
- 裁决成功后，`active_flow` 才可能更新；换句话说，`active_flow` 是后端物化结果，`frame` 只是输入信号。

冲突处理规则：

| 当前 active_flow | LLM frame_hint | 后端裁决 |
|---|---|---|
| `upload_collecting` | `job_search` / `candidate_search` | 不复制冲突逻辑；物化为 `active_flow=upload_conflict` + `pending_interruption=<本轮候选 frame/slots>`，完全交给现有 `_route_upload_conflict` / `_enter_upload_conflict` 路径处理 |
| `upload_conflict` | 任意 frame | 优先解析用户对冲突确认的选择 |
| `search_active` | `job_upload` / `resume_upload` | 清搜索快照后进入上传流程，或按冲突规则确认 |
| `idle` | 任意合法 frame | 可接受为新 frame，并物化到对应 handler |

这样避免两套状态漂移：session 里只持久化后端状态，LLM 的 frame 不作为长期状态保存，最多写入日志用于观测。

### 3.2 frame 命名

避免 `worker_search` 与用户角色 `worker` 混淆，建议 frame 命名如下：

| frame | 含义 |
|---|---|
| `job_search` | 找岗位，通常 worker 使用，broker 也可使用 |
| `candidate_search` | 找求职者 / 找工人，factory 和 broker 使用 |
| `job_upload` | 发布岗位 |
| `resume_upload` | 上传简历 |
| `none` | 闲聊、命令或无法归类 |

### 3.3 LLM 输出与后端裁决要拆开

不要让 LLM 直接输出最终 `merge_policy` 并写 session。否则只是把 Bug 5 里的 `add/update` 改名成 `replace/add`，歧义又回来了。

建议拆成两层 DTO：

LLM 解析结果 `DialogueParseResult`：

```json
{
  "dialogue_act": "start_search | modify_search | answer_missing_slot | show_more | start_upload | cancel | reset | chitchat",
  "frame_hint": "job_search | candidate_search | job_upload | resume_upload | none",
  "slots_delta": {
    "city": ["北京市"]
  },
  "merge_hint": {
    "city": "replace | add | remove | unknown"
  },
  "needs_clarification": false,
  "confidence": 0.92
}
```

后端裁决结果 `DialogueDecision`：

```json
{
  "dialogue_act": "modify_search",
  "resolved_frame": "job_search",
  "accepted_slots_delta": {
    "city": ["北京市"]
  },
  "resolved_merge_policy": {
    "city": "replace"
  },
  "final_search_criteria": {
    "city": ["北京市"],
    "job_category": ["餐饮"],
    "salary_floor_monthly": 2500
  },
  "missing_slots": [],
  "route_intent": "follow_up"
}
```

实施原则：

- LLM 的 `merge_hint` 只能作为弱信号。
- prompt 应要求：只有用户明确表达替换 / 追加 / 删除时才输出 `replace/add/remove`；裸城市、短句或模糊表达统一输出 `unknown`。例如“换成苏州” → `replace`，“苏州也行” → `add`，“苏州” / “苏州有吗” → `unknown`。
- `resolved_merge_policy` 由后端 reducer 决定。
- `resolved_merge_policy` 只对 `accepted_slots_delta` 中存在的 key 有意义；没有变化的字段不写 `keep`，保留是隐式行为。
- 如果“replace 还是 add”无法稳定判断，返回 `needs_clarification=true`，反问用户，而不是偷偷选一个。
- reducer 可以覆盖 LLM 的 `needs_clarification`：当 `confidence < 0.6` 且本轮涉及关键字段（`city` / `job_category` / `salary_*`），或 `frame_hint` 与 `active_flow` 冲突且无法按 §3.1 消解时，强制 `needs_clarification=true`。

Reducer 责任清单：

| 必做 | 必不做 |
|---|---|
| schema 校验 `accepted_slots_delta` | 调 LLM |
| 决定 `resolved_merge_policy` | 写 history |
| 重算 `missing_slots` | 跨 frame 污染 `search_criteria` |
| 物化 `active_flow` 转移 | 直接覆盖 session，必须先形成 decision |
| 处理 `active_flow` / `frame_hint` 冲突 | 复制 `message_router` 已有冲突 handler 逻辑 |

### 3.4 “北京有吗”这类歧义如何处理

不能把“北京有吗”固定写成 replace。建议按可配置策略处理：

1. 如果文本有明确追加信号，如“也行 / 也可以 / 加上 / 还看”，裁决为 add。
2. 如果文本有明确替换信号，如“换成 / 改成 / 只看 / 不看原来的”，裁决为 replace。
3. 如果只有 “X 有吗 / X 有没有”，且当前已有不同城市：
   - 若产品希望默认替换，可配置为 replace，并在文案上体现“已为您切换到北京”；
   - 若产品希望避免误解，可配置为 clarify，追问“是只看北京，还是北京和原城市都看？”；
   - 不建议让 LLM 单独决定，因为这会回到 Bug 5 的歧义来源。

默认建议：`X 有吗` 在存在旧城市时走 clarify，除非后续产品明确选择“默认替换”。

## 4. search 流程也需要 awaiting 状态

当前 `awaiting_field` 主要由上传流程设置。若搜索流程返回：

```text
信息还不够完整，请补充：月薪下限
```

但 session 没有记录 `awaiting_field=salary_floor_monthly`，下一轮用户只发 `2500` 时，`answer_missing_slot` 就没有可靠落点。

阶段一必须补齐搜索 awaiting 物化：

- 当 `_handle_search` 因缺字段追问时，写入 `awaiting_fields=missing`（FIFO 队列），并记录 awaiting 所属 frame，例如 `awaiting_frame=job_search`。
- awaiting TTL 可以复用上传草稿 TTL（当前约 10 分钟），也可以单独配置 `search_awaiting_ttl_seconds`；无论采用哪种，都必须在 session 中记录 `awaiting_expires_at`，过期后裸值不再按补槽处理。
- 如果不想复用上传字段，新增 `pending_search` 或 `search_awaiting_field`，避免与上传草稿混淆。
- 下一轮如果 `dialogue_act=answer_missing_slot`，先检查 awaiting 队列是否存在且未过期。
- awaiting 只作为裸值 / 歧义值的 tie-breaker：例如只发 `2500` 时填薪资字段；如果用户发 `北京普工`，LLM/规则已经抽出 `city + job_category`，应直接按 `slots_delta` 合并，不需要机械消费队列。
- 裸数值不应纯按队首消费，应先按字段类型和取值范围匹配：例如 `awaiting_fields=[salary_floor_monthly, headcount]` 时，`2` 更像 `headcount`，`2500` 更像薪资。这个判断应复用或对齐 `intent_service._normalize_int_field` 的范围校验。
- 用户一次补齐多个字段时，按 `slots_delta` 派发到对应字段；补齐后从 awaiting 队列中移除已接受字段。
- 一旦搜索成功、用户重置、进入上传、或确认新流程，清掉搜索 awaiting。

没有这一步，“2500 优先解释为上一轮追问字段”只是文档愿望，落不了地。

## 5. 失败模式与降级策略

| 失败模式 | 建议处理 |
|---|---|
| LLM JSON 解析失败 | shadow 阶段忽略新结果；primary 阶段优先回退旧 `classify_intent`，再失败才 chitchat / system busy |
| `dialogue_act=answer_missing_slot` 但 session 无 awaiting | 如果 `slots_delta` 有有效字段，按 `modify_search` / `start_search` 重新裁决；否则反问或 chitchat |
| `slots_delta` 字段不属于 frame schema | drop 非法字段并打日志；若 drop 后无有效字段且本轮需要业务动作，走 clarification |
| 给了 `merge_hint` 但没有对应 slot | 忽略该 merge_hint |
| 有 slot 但没有 merge_hint | reducer 按 schema 默认策略处理；列表字段在有旧值且语义不明时可配置 clarify |
| frame_hint 与 active_flow 冲突 | 参见 §3.1 冲突处理表；active_flow 优先，不让 LLM 直接覆盖后端状态 |
| LLM 给 `start_upload`，但角色无权限 | 后端权限优先，拒绝或转成可用 frame |
| 新 DTO 缺关键字段 | 按缺失字段的最小安全降级处理，不写 session |
| `needs_clarification=true` 但 LLM 未给澄清文案 | 不使用 LLM 文案；由 frame/slot schema 的澄清模板生成结构化追问 |
| `dialogue_act=cancel` 但当前无可取消流程 | 不改 session；返回友好提示（如“当前没有进行中的草稿/搜索需要取消”）或按 chitchat 处理 |

基本原则：LLM 输出永远不能直接改 session；所有写入都必须经过 schema 校验和 reducer 裁决。该原则在 §9 也作为非目标再次声明。

## 6. 兼容层实施方案

### 6.1 prompt 策略

不建议让 LLM 同时输出新旧两套字段，容易出现互相矛盾。

推荐分阶段：

1. legacy mode：继续使用当前 `IntentResult` prompt 和路由。
2. shadow mode：生产仍走 legacy；旁路调用新 prompt，记录 `DialogueParseResult`、`DialogueDecision` 和 legacy 结果差异，不影响用户。
3. dual-read gated mode：按 user hash 或配置白名单让一小部分请求使用新 DTO 派生的 legacy `IntentResult` 路由。
4. primary mode：新 DTO 作为主链路；旧 `IntentResult` 仅作为 fallback。

shadow mode 会带来额外 LLM 调用成本，不应默认全量开启。建议先按比例采样（例如 5%-10%）或仅对白名单用户开启，并避开高峰 QPS 时段；日志字段中记录 shadow 是否执行，便于评估成本和收益。

### 6.2 旧 IntentResult 如何派生

新 DTO 不直接替换所有 handler。先由兼容层按 `(dialogue_act, resolved_frame, active_flow)` 三维派生旧结构，避免把 `start_search`、`modify_search`、`answer_missing_slot` 都压成同一个 `search_job`：

| dialogue_act | resolved_frame | active_flow / 上下文 | IntentResult 派生 |
|---|---|---|---|
| `start_search` | `job_search` | `idle` 或无有效搜索上下文 | `intent=search_job`，`structured_data=accepted_slots_delta` |
| `start_search` | `candidate_search` | `idle` 或 broker/factory 切到找人 | `intent=search_worker` |
| `modify_search` | `job_search` | `search_active` 或已有 `search_criteria` | `intent=follow_up`，`structured_data=final_search_criteria` |
| `modify_search` | `candidate_search` | `search_active` 或已有 `search_criteria` | `intent=follow_up`，`structured_data=final_search_criteria` |
| `answer_missing_slot` | `job_search` / `candidate_search` | 存在搜索 awaiting 或已有搜索上下文 | `intent=follow_up`，`structured_data=final_search_criteria` |
| `start_upload` | `job_upload` | 允许发布岗位 | `intent=upload_job` |
| `start_upload` | `resume_upload` | 允许上传简历 | `intent=upload_resume` |
| `show_more` | 任意 | 有候选快照 | `intent=show_more` |
| `cancel` / `reset` | 任意 | 按命令语义 | `intent=command` 或直接走命令 handler |
| `chitchat` | `none` | 任意 | `intent=chitchat` |

注意：

- follow_up 的兼容派生必须继续使用“完整 criteria 快照”，不要恢复 `criteria_patch` op 语义。
- 对已有搜索的 `modify_search` / `answer_missing_slot` 派生为 `follow_up`，不是 `search_job`。否则兼容层会丢失对话行为语义，使“补槽/改条件”和“新搜索”不可区分。
- `route_intent` 是兼容层输出，不等同于 LLM 的 `dialogue_act`。

### 6.3 灰度与老 session

- 灰度粒度：优先按 `external_userid` hash 或白名单，避免同一个用户半段对话在两套理解层之间来回切。
- shadow 采样：可按百分比扩大，但只写日志，不写 session。
- 老 session：不需要迁移。新 reducer 从现有 `active_flow/search_criteria/pending_upload/awaiting_field` 推导上下文。
- 如新增 `awaiting_frame`、`awaiting_fields` 或 `pending_search` 字段，必须在 `SessionState`（当前是 Pydantic model）上使用 `Field(default=...)` / `default_factory` 提供默认值；旧 Redis session 缺字段时应按默认填充，不能反序列化失败。
- 所有 `conftest` / unit test 里的 `SessionState` fixture 同步补默认字段，避免测试路径和 Redis 反序列化路径表现不一致。

## 7. golden conversation 格式

建议新增 fixture 目录：

```text
backend/tests/fixtures/dialogue_golden/
```

示例 YAML：

```yaml
id: worker_restaurant_city_change
role: worker
initial_session:
  active_flow: idle
  broker_direction: null
  search_criteria: {}
  awaiting_frame: null
  awaiting_fields: []
  pending_upload: {}
  pending_upload_intent: null
turns:
  - user: "西安，想找个饭店的服务员的工作"
    expect:
      dialogue_act: start_search
      resolved_frame: job_search
      accepted_slots_delta:
        city: ["西安市"]
        job_category: ["餐饮"]
      search_criteria:
        city: ["西安市"]
        job_category: ["餐饮"]
      route_intent: search_job
      should_ask: false

  - user: "2500"
    expect:
      dialogue_act: answer_missing_slot
      resolved_frame: job_search
      accepted_slots_delta:
        salary_floor_monthly: 2500
      search_criteria:
        city: ["西安市"]
        job_category: ["餐饮"]
        salary_floor_monthly: 2500
      route_intent: follow_up

  - user: "北京有吗"
    product_policy:
      ambiguous_city_query: clarify
    expect:
      dialogue_act: modify_search
      resolved_frame: job_search
      needs_clarification: true
      clarification:
        kind: city_replace_or_add
        ambiguous_field: city
        options: ["replace", "add"]
```

断言 API 建议：

```python
run_dialogue_case(path).assert_turns()
```

每轮至少断言：

- `dialogue_act`
- `resolved_frame`
- `accepted_slots_delta`
- merge 后的 `search_criteria`
- `missing_slots` / 是否追问
- `clarification.kind` / `ambiguous_field` / `options`，避免用易碎的文案包含断言
- 最终路由到 `search_jobs` / `search_workers` 的 criteria

golden 套不只覆盖 happy path，首批还应包含反例 / 降级 case：

- 角色权限拒绝：worker 尝试 `job_upload`、factory 尝试 `job_search`。
- `active_flow` 冲突：`upload_collecting` 中用户说“先找工人”，应进入 `upload_conflict` 而不是直接切流程。
- LLM JSON 解析失败：primary 阶段能 fallback 到 legacy `IntentResult` 并继续跑通。
- 低 confidence：关键字段低置信度触发 clarification。
- awaiting 过期：过期后用户再发 `2500`，不得继续污染旧搜索补槽。

## 8. 阶段路线与退出标准

### 阶段一：收紧现有链路

已覆盖：

- 搜索字段 `expected_* -> city/job_category` 重映射。
- 城市短名 / 别名归一。
- follow_up 全量 criteria 快照，避免 `criteria_patch` op 歧义。
- broker 找工人关键链路修复。
- Bug 6 引入的 `_WORKER_SEARCH_SIGNALS` / `_JOB_POSTING_SIGNALS` / `_CITY_ADD_SIGNALS` / `_CITY_REPLACE_SIGNALS` 属于阶段一允许的临时护栏，进入阶段二后应由 `dialogue_act` / `merge_hint` / reducer 接管；这些 signals 已纳入 [keyword-rules-audit.md](keyword-rules-audit.md) 的 MEDIUM 跟踪项。

仍待补齐：

- worker 搜索信号护栏：worker + “找/想找/求职/工作”等信号必须落 `job_search`，这解决的是 intent/frame 误判，不是 missing 重算。
- 搜索流程写入 awaiting 状态，否则 `2500` 这类 answer_missing_slot 无法稳定落地。
- 搜索场景最终 `missing_fields` 由后端 schema 重算；但前提是 frame 已被纠正。
- provider 消费 `session_hint`，至少让 LLM 看到 `active_flow`、`awaiting_field`、`search_criteria` 摘要。

退出标准：

- worker “西安饭店服务员 → 2500 → 北京有吗” golden case 通过，并作为 regression 防护保留。
- broker “机械厂普工 → 北京 → 换成苏州” golden case 通过，并作为 regression 防护保留。
- `keyword-rules-audit.md` 中 HIGH 风险项有明确接管计划。
- 新增/更新单测覆盖 intent 误判、短句补槽、城市归一、follow_up 替换。

### 阶段二：引入 DialogueParseResult / DialogueDecision

目标：

- 新 DTO 先 shadow，不影响线上路由。
- reducer 输出 `DialogueDecision`，后端从 decision 派生旧 `IntentResult`。
- 接管 [keyword-rules-audit.md](keyword-rules-audit.md) 中 3 个 HIGH 风险项：cancel / proceed / resume 这类开放自然语言选择，不再只靠关键词列表。

### 阶段三：统一 Slot Schema

用 schema 描述每个 frame 的字段：

- 字段名、类型、归一化方式
- 是否必填、是否可追问
- 默认合并策略
- 角色权限
- 追问文案

`missing_slots`、字段清洗、追问文案和权限判断都从 schema 派生。

### 阶段四：扩大灰度并替换旧链路

- 从 shadow 到白名单，再到 userid hash 百分比。
- primary mode 运行稳定后，旧 `criteria_patch` 只保留兼容 fallback。
- 删除或降级不再需要的开放式关键词规则。

## 9. 非目标

为避免范围失控，以下不作为本轮目标：

- 不做完整学术意义上的 DST（dialogue state tracking），只做招聘业务需要的 frame/slot/reducer。
- 不支持多 frame 嵌套，例如一条消息同时完整发布岗位又完整搜索岗位，仍按现有冲突/顺带搜索流程拆解。
- 不让 LLM 直接写 session。
- 不通过无限扩中文关键词表来解决开放自然语言选择。
- 不要求迁移所有旧 Redis session；新字段必须默认兼容。

## 10. 结论

当前系统已有 `active_flow` 状态机和 session 底座，`fdeb18d` 也修掉了一批搜索/follow_up 事故点。下一步的重点不是继续堆 prompt，而是引入一个清晰的裁决层：

```text
用户文本 + SessionState
  -> LLM 抽取（含确定性兜底）DialogueParseResult
  -> 后端 reducer 裁决 DialogueDecision
  -> schema 校验、missing 重算、状态物化
  -> 兼容派生 IntentResult 并路由
```

核心原则是：LLM 负责理解语言，后端负责状态一致性和业务裁决。
