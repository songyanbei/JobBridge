# 多轮上传 Stage C1/C2 开发实施与验收说明

> 面向开发分发。Stage C 拆成两段：C1 先做兼容式状态机落地，C2 再做 `SessionState` 嵌套结构收敛。这样先拿到行为收益，同时降低对 Stage A/B 测试和旧 Redis session 的冲击。

---

## 1. 拆分原则

原 Stage C 混合了两类工作：

```text
行为收敛：
  让 active_flow 接管路由，明确 upload/search/chitchat/command/切流程的状态边界。

结构收敛：
  把 pending_*、search_criteria、history 等扁平字段迁移为 UploadDraft / SearchState / llm_context_window。
```

推荐拆分：

```text
Stage C1：兼容式状态机落地
Stage C2：SessionState 结构收敛
```

C1 必须满足状态机的行为验收；C2 再完成数据结构清理。不要在 C1 一次性重写所有 session 字段。

PR 节奏：

```text
1. C1 单独一个 PR，合入主线后在 mock-testbed 跑 1-2 天。
2. C1 稳定后再开 C2 PR。
3. 不要把 C1/C2 塞进同一个 PR。
```

---

## 2. Stage C1：兼容式状态机落地

### 2.1 目标

```text
1. active_flow 成为路由裁决源。
2. 上传追问、搜索追问、闲聊、命令、切流程不再共用模糊 follow_up 入口。
3. pending upload 生命周期完整：补字段、取消、超时、max rounds、冲突确认、完成。
4. upload_and_search 支持有结果和 0 命中两条路径。
5. Stage A/B 的扁平字段和测试尽量不动。
```

### 2.2 改动范围

```text
backend/app/schemas/conversation.py
backend/app/services/conversation_service.py
backend/app/services/message_router.py
backend/app/services/upload_service.py
backend/app/services/search_service.py
backend/app/services/intent_service.py
backend/tests/unit/
backend/tests/integration/
```

可选新增：

```text
backend/app/services/session_flow_service.py
```

如果新增 `session_flow_service.py`，只放状态 helper，不放搜索查询和 LLM 调用。

### 2.3 SessionState 新增字段

保留 Stage A/B 扁平字段，新增 C1 字段：

```python
active_flow: str | None = None
# idle / upload_collecting / upload_conflict / search_active

last_intent: str | None = None
# 与 current_intent 双写；只用于观测和日志，不参与路由

pending_interruption: dict | None = None
# upload_conflict 中保存的新意图瘦身版

failed_patch_rounds: int = 0
# 精细失败补字段计数；follow_up_rounds 保留兼容

last_criteria: dict = Field(default_factory=dict)
# 不论搜索是否命中，都记录本次有效 criteria
# C2 收敛到 SearchState.last_criteria；C1 保持顶层，避免破坏现有 session 反序列化
```

继续保留：

```python
current_intent
pending_upload
pending_upload_intent
awaiting_field
pending_started_at
pending_updated_at
pending_expires_at
pending_raw_text_parts
follow_up_rounds
search_criteria
candidate_snapshot
shown_items
broker_direction
history
```

### 2.4 load_session 推导 active_flow

只对旧 session 推导 `active_flow`。如果 session 已有 `active_flow`，不要每次重新推导，避免旧字段反向污染状态机。

推导规则：

```text
active_flow 缺失或 None：
  pending_upload_intent 非空 -> upload_collecting
  candidate_snapshot 存在 -> search_active
  否则 -> idle
```

不要用 `pending_upload` dict 是否非空做推导依据。Stage A 的有效 pending 判断以 `pending_upload_intent` 为准；如果 `pending_upload` 残留字段但 `pending_upload_intent` 已被清空，应视为 zombie pending 并清理。

执行频率：

```text
推导：只在 active_flow 缺失或 None 时跑一次，写回 session。
状态修复：每次 load_session 都跑一次，用于修正 active_flow 与扁平字段不一致的组合。
```

状态修复规则：

```text
active_flow=upload_collecting 但 pending_upload_intent 为空 -> 修复为 idle
active_flow=idle 但 pending_upload 残留 -> 清 pending
active_flow=search_active 但 candidate_snapshot 为空 -> 降为 idle，但保留 last_criteria
active_flow=upload_conflict 但 pending_interruption 为空 -> 回 upload_collecting 或 idle，按 pending 是否存在决定
```

### 2.5 路由结构

文件：`backend/app/services/message_router.py`

新增四个路由函数：

```python
def _route_idle(intent_result, msg, user_ctx, session, db): ...
def _route_upload_collecting(intent_result, msg, user_ctx, session, db): ...
def _route_upload_conflict(intent_result, msg, user_ctx, session, db): ...
def _route_search_active(intent_result, msg, user_ctx, session, db): ...
```

现有 `_handle_upload`、`_handle_search`、`_handle_follow_up`、`_handle_show_more` 复用为 helper，不必一次性重写。

主流程：

```python
intent_result = classify_intent(...)

session.last_intent = intent_result.intent

# C1 兼容旧 attach_image：upload_collecting 期间 current_intent 钉在原上传意图，
# 避免“2个人”被识别成 follow_up 后污染 current_intent。
if session.active_flow == "upload_collecting" and session.pending_upload_intent:
    session.current_intent = session.pending_upload_intent
else:
    session.current_intent = intent_result.intent

if intent_result.intent == "command":
    return _route_command(intent_result, msg, user_ctx, session, db)

if session.active_flow == "upload_collecting":
    return _route_upload_collecting(...)
if session.active_flow == "upload_conflict":
    return _route_upload_conflict(...)
if session.active_flow == "search_active":
    return _route_search_active(...)
return _route_idle(...)
```

约束：

```text
active_flow 是主路由裁决源。
current_intent 不再决定主路由。
pending_upload_intent 仅用于完成上传时恢复 origin intent。
```

命令与状态机的边界：

| 命令类型 | 例子 | C1 行为 |
|---|---|---|
| 全局型命令 | `/帮助`、`/我的状态`、`/人工客服`、`/续期`、`/下架`、`/招满了`、`/删除我的信息` | 不受 `active_flow` 影响，直接走 `command_service` |
| 状态相关命令 | `/找岗位`、`/找工人`、`/重新找`、`/取消` | 先检查 `active_flow`，再决定是否进入状态机分支 |

状态相关命令规则：

```text
upload_collecting + /找岗位 或 /找工人
  -> 进入 upload_conflict，见 §2.9

upload_collecting + /重新找
  -> 清 search_criteria/candidate_snapshot/shown_items
  -> 不清 pending_upload
  -> 回复“搜索条件已重置；您仍在发布岗位（缺 X），请继续补充或发 /取消 放弃。”

upload_collecting + /取消
  -> 与自然语言 cancel 等价，清 pending 并回 idle
```

### 2.6 upload_collecting 行为

处理顺序：

```text
1. timeout 检查
2. cancel / abandon
3. chitchat
4. new business intent -> upload_conflict
5. field patch
```

字段补全：

```text
structured_data -> criteria_patch -> 正则解析
```

有效 patch：

```text
merge pending_upload
append pending_raw_text_parts
重算 missing
```

字段齐全：

```text
raw_text = "\n".join(pending_raw_text_parts)
调用 upload_service.process_upload
after_commit=none -> active_flow=idle
after_commit=search 且有结果 -> active_flow=search_active
after_commit=search 且0命中 -> active_flow=idle
```

不论搜索是否命中，都写 `last_criteria`。

`failed_patch_rounds` 递增条件：

```text
1. 三层抽取（structured_data / criteria_patch / 正则）都拿不到 awaiting_field 的有效值。
2. 抽到值但类型或范围非法，例如 headcount <= 0、salary 非数字。
```

以下情况不递增：

```text
chitchat / cancel / new intent / 补了其他有效上传字段。
```

C1 max rounds 退出依据：

```text
failed_patch_rounds >= 2
  -> clear pending_upload
  -> active_flow = idle
  -> 回复整段重发提示
```

`follow_up_rounds` 仅作为 Stage A 兼容计数器保留，不再决定 C1 的退出。

### 2.7 upload_conflict 行为

进入条件：

```text
upload_collecting 中用户明确表达 search_job / search_worker
或表达不同上传流程
```

`pending_interruption` 瘦身结构：

```python
{
    "intent": intent_result.intent,
    "structured_data": intent_result.structured_data,
    "criteria_patch": intent_result.criteria_patch,
    "raw_text": msg.content or "",
}
```

确认选项：

| 用户选择 | 行为 |
|---|---|
| 继续发布 | `active_flow=upload_collecting` |
| 先找工人/岗位 | 清 pending，执行 `pending_interruption` |
| 取消草稿 | 清 pending，`active_flow=idle` |

不做多草稿暂存。

C1 强规则识别用户回复：

| 用户表达 | 行为 |
|---|---|
| 包含“继续”“继续发布”“接着发” | 回 `upload_collecting` |
| 命中 cancel 强规则 | 清 pending，回 `idle` |
| 包含“先找”“找工人”“找岗位”“看看”“简历”“岗位”，或 LLM intent in (`search_job`, `search_worker`) | 执行 `pending_interruption` |
| 其他 | 重复确认提示；最多 1 次后清草稿回 `idle`，避免死循环 |

注意：“换”不能单独作为执行 `pending_interruption` 的触发词，必须同时出现搜索指向词，例如“换一批简历”“换找岗位”。

执行 `pending_interruption` 时不再重新调用 LLM：

```text
1. 用 pending_interruption.intent / structured_data / criteria_patch 直接构造 IntentResult。
2. raw_text 仅用于日志和 search raw_query。
3. 分发到对应 `_handle_search` / `_handle_upload` / `_handle_upload_and_search`。
```

### 2.8 search_active 行为

```text
follow_up -> merge criteria -> search
show_more -> 使用 candidate_snapshot
chitchat -> 保留 search state，闲聊回复
new upload -> 清 candidate_snapshot / shown_items，保留 search_criteria 与 last_criteria，进入上传流程
reset_search -> 清 search_criteria/candidate_snapshot/shown_items，不清 pending_upload
```

搜索 0 命中：

```text
candidate_snapshot=None
active_flow=idle
last_criteria 保留
```

### 2.9 broker 方向切换

broker 在 `upload_collecting` 中发 `/找岗位` 或 `/找工人`：

```text
Stage C1：进入 upload_conflict
pending_interruption.intent = search_job/search_worker
回复确认：继续发布 / 先找岗位或工人 / 取消草稿
```

这会改变 Stage B 中“保留 pending 并提醒”的行为，属于按 C1 spec 的演进，不算核心测试回退。

### 2.10 attach_image 兼容迁移

C1 规则：

```text
优先：session.active_flow == "upload_collecting"
回落：session.current_intent in ("upload_job", "upload_resume", "upload_and_search")
```

回落只用于旧 session 兼容。C2 删除回落。

### 2.11 LLM session_hint 占位

C1 只加形参和构造 helper，不强制改 prompt：

```python
def classify_intent(
    text: str,
    role: str,
    history: list[dict] | None = None,
    current_criteria: dict | None = None,
    user_msg_id: str | None = None,
    session_hint: dict | None = None,  # C1 新增；provider 可忽略
) -> IntentResult:
    ...
```

```python
def build_session_hint(session: SessionState) -> dict:
    return {
        "active_flow": session.active_flow,
        "awaiting_field": session.awaiting_field,
        "pending_upload_intent": session.pending_upload_intent,
        "pending_upload": session.pending_upload,
    }
```

如果 provider 接口暂不消费 `session_hint`，可以先只记录或传空，避免扩大风险。

---

## 3. Stage C1 必测用例

| Case | 期望 |
|---|---|
| 旧 session 无 active_flow | load 后推导为 idle/search_active/upload_collecting |
| active_flow 已存在 | 不被旧字段反复推导覆盖 |
| 推导与状态修复 | 推导只在 active_flow 缺失时跑；self-healing 每次 load 都可修复 |
| pending_upload 空但 active_flow=upload_collecting | 修复为 idle |
| “招厨师7500” -> “2人” | 入库成功，active_flow=idle |
| upload_collecting 闲聊 | pending 保留，failed_patch_rounds 不变 |
| failed_patch_rounds 连续答非所问 2 次 | 清 pending，active_flow=idle |
| failed_patch_rounds 遇到 chitchat | 不递增 |
| upload_collecting cancel | 清 pending，active_flow=idle |
| upload_collecting timeout | 清 pending 或提示整段重发 |
| upload_collecting 搜索意图 | 进入 upload_conflict |
| upload_conflict 继续发布 | 回 upload_collecting |
| upload_conflict 先找工人 | 清 pending，执行 pending_interruption |
| pending_interruption 序列化往返 | model_dump -> Redis -> model_validate 后字段保持 |
| upload_and_search 有结果 | 入库 + active_flow=search_active |
| upload_and_search 0 命中 | 入库 + active_flow=idle + last_criteria 保留 |
| search_active show_more | 使用 snapshot |
| search_active 中新 upload | 清 snapshot/shown_items，保留 criteria/last_criteria |
| broker /找岗位 during upload_collecting | 进入 upload_conflict |
| attach_image during upload_collecting | 能挂载，且旧 session 回落仍可用 |

建议运行：

```powershell
cd backend
pytest tests/unit/test_conversation_service.py tests/unit/test_message_router.py tests/unit/test_upload_service.py tests/unit/test_search_service.py
pytest tests/integration/test_phase3_upload_and_search.py
```

---

## 4. Stage C1 验收标准

1. `active_flow` 成为主路由裁决源。
2. `last_intent` 记录本轮 LLM 意图；`upload_collecting` 期间 `current_intent` 钉在 `pending_upload_intent`，且 `current_intent` 不参与主路由。
3. 上传和搜索的 follow_up 行为分离。
4. pending upload 生命周期完整：创建、补字段、取消、超时、failed_patch max rounds、完成。
5. `upload_conflict` 可处理上传中切搜索或切上传。
6. `upload_and_search` 支持有结果和 0 命中。
7. `last_criteria` 在有结果和 0 命中时都写入。
8. `attach_image` 优先使用 `active_flow`，回落 `current_intent`。
9. 状态相关命令不会绕过状态机边界。
10. Stage A/B 核心链路不回退。

---

## 5. Stage C2：SessionState 结构收敛

### 5.1 目标

C2 在 C1 行为稳定后执行，目标是清理技术债：

```text
pending_* -> UploadDraft
search_criteria/candidate_snapshot/shown_items/broker_direction/last_criteria -> SearchState
history -> llm_context_window
current_intent -> 删除或停止读取
```

### 5.2 改动范围

```text
backend/app/schemas/conversation.py
backend/app/services/conversation_service.py
backend/app/services/message_router.py
backend/app/services/upload_service.py
backend/app/services/search_service.py
backend/tests/unit/
backend/tests/integration/
```

### 5.3 目标结构

```python
class SessionState(BaseModel):
    role: str
    active_flow: str = "idle"
    last_intent: str | None = None
    pending_upload: UploadDraft | None = None
    pending_interruption: PendingInterruption | None = None
    search_state: SearchState = Field(default_factory=SearchState)
    llm_context_window: list[dict] = Field(default_factory=list)
    updated_at: str = ""
```

```python
class UploadDraft(BaseModel):
    entity_type: str
    origin_intent: str
    data: dict = Field(default_factory=dict)
    raw_text_parts: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    awaiting_field: str | None = None
    follow_up_rounds: int = 0
    failed_patch_rounds: int = 0
    after_commit: str = "none"
    created_at: str
    updated_at: str
    expires_at: str
```

```python
class SearchState(BaseModel):
    criteria: dict = Field(default_factory=dict)
    last_criteria: dict = Field(default_factory=dict)
    candidate_snapshot: CandidateSnapshot | None = None
    shown_items: list[str] = Field(default_factory=list)
    broker_direction: str | None = None
```

### 5.4 迁移策略

迁移逻辑放在 `conversation_service.load_session` 或专用 migration helper：

```text
旧 pending_upload dict + pending_* 字段 -> UploadDraft
旧 search_criteria/candidate_snapshot/shown_items/broker_direction/last_criteria -> SearchState
旧 history -> llm_context_window
旧 current_intent -> last_intent，仅迁移一次
```

过渡策略：

```text
1. load_session 时尝试一次性迁移旧扁平字段 -> 嵌套结构。
2. 迁移成功后立即 save_session 写回 Redis，覆盖为新结构。
3. 保留迁移分支至少 1 个 Redis TTL 周期（当前默认 30 分钟），让剩余旧 session 自然过期或被迁移。
4. C2 合主线稳定 24 小时后，再删除 load_session 中的旧字段迁移分支代码。
```

### 5.5 代码清理

C2 完成后：

```text
1. 删除或停止读取 pending_upload_intent / awaiting_field / pending_* 扁平字段。
2. 删除或停止读取 search_criteria / candidate_snapshot / shown_items 顶层字段。
3. 删除 attach_image 对 current_intent 的回落判断。
4. 将 record_history 改名或包一层为 record_context_window。
5. 文档和测试统一使用嵌套结构。
```

---

## 6. Stage C2 必测用例

| Case | 期望 |
|---|---|
| 旧扁平 session 迁移 | 转成 UploadDraft/SearchState |
| 旧扁平 session 迁移写回 | load 后 save_session 写回嵌套结构 |
| 新嵌套 session 保存读取 | model_dump/load 后结构不变 |
| old history | 迁移到 llm_context_window |
| attach_image | 不依赖 current_intent |
| pending 补字段 | 使用 UploadDraft 字段 |
| search follow_up | 使用 SearchState.criteria |
| show_more | 使用 SearchState.candidate_snapshot |
| Redis TTL 兼容期后 | 可移除旧字段读取逻辑 |

---

## 7. Stage C2 验收标准

1. `SessionState` 使用嵌套 `UploadDraft/SearchState`。
2. 主链路不再读取扁平 pending/search 字段。
3. `history` 语义收敛为 `llm_context_window`。
4. `current_intent` 不再被 `attach_image` 或路由读取。
5. C1 所有行为验收继续通过。
6. 旧 Redis session 有明确迁移或自然过期策略。
