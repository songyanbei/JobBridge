# 多轮上传 Stage C 开发实施与验收说明

> 面向开发分发。目标是把 Stage A/B 的过渡字段和补丁式分支，收敛为显式 `active_flow + UploadDraft + SearchState` 状态机。

---

## 1. 目标

Stage C 解决的是长期可维护性：

```text
1. 上传追问、搜索追问、闲聊、命令、切流程不再共用模糊 follow_up 入口。
2. pending upload 有完整生命周期：创建、补字段、取消、超时、max rounds、冲突确认、完成。
3. upload_and_search 入库成功后能稳定接续搜索，并处理 0 命中。
4. 图片附件挂载从 current_intent 迁移到 active_flow。
```

---

## 2. 改动范围

建议改动范围：

```text
backend/app/schemas/conversation.py
backend/app/services/conversation_service.py
backend/app/services/message_router.py
backend/app/services/upload_service.py
backend/app/services/search_service.py
backend/app/services/intent_service.py
backend/app/llm/prompts.py
backend/tests/unit/
backend/tests/integration/
```

可选新增：

```text
backend/app/services/search_orchestrator.py
backend/app/services/session_flow_service.py
```

若引入新文件，职责必须清晰：状态转移放 `session_flow_service`，搜索默认条件和 fallback 编排放 `search_orchestrator`。

---

## 3. 目标模型

### 3.1 SessionState

文件：`backend/app/schemas/conversation.py`

长期结构：

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

`active_flow` 允许值：

```text
idle
upload_collecting
upload_conflict
search_active
```

`last_intent` 只用于观测和日志，不参与路由决策。

### 3.2 UploadDraft

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

时间字段使用 ISO 8601 UTC 字符串，比较前转 `datetime`。

### 3.3 SearchState

```python
class SearchState(BaseModel):
    criteria: dict = Field(default_factory=dict)
    last_criteria: dict = Field(default_factory=dict)
    candidate_snapshot: CandidateSnapshot | None = None
    shown_items: list[str] = Field(default_factory=list)
    broker_direction: str | None = None
```

### 3.4 PendingInterruption

```python
class PendingInterruption(BaseModel):
    intent: str
    structured_data: dict = Field(default_factory=dict)
    criteria_patch: list[dict] = Field(default_factory=list)
    raw_text: str = ""
```

用于 `upload_conflict`。用户选择“先找工人/先找岗位”后，将它还原为 `IntentResult` 所需字段重新分发。

---

## 4. 迁移步骤

### 4.1 双写 current_intent 和 last_intent

第一步不要直接删 `current_intent`。

```text
1. 新增 last_intent。
2. 路由中同时写 current_intent 和 last_intent。
3. 日志、观测、后续新逻辑读取 last_intent。
4. attach_image 迁移完成后再停止读取 current_intent。
```

### 4.2 引入 active_flow，但保留旧字段兼容

兼容策略：

```text
旧 session 没有 active_flow -> 默认 idle
旧 session 有 pending_upload dict -> 可迁移为 UploadDraft 或按空草稿处理
旧 session 有 search_criteria -> 放入 search_state.criteria
旧 session 有 candidate_snapshot/shown_items -> 放入 search_state
```

迁移逻辑应放在 `conversation_service.load_session` 或专用 migration helper 中。

### 4.3 attach_image 迁移

当前逻辑依赖：

```text
session.current_intent in ("upload_job", "upload_resume", "upload_and_search")
```

Stage C 改为：

```text
session.active_flow == "upload_collecting"
OR 最近成功上传实体仍在可挂载窗口内
```

迁移后再删除或停止使用 `current_intent`。

---

## 5. 路由状态机

文件：`backend/app/services/message_router.py`

推荐主流程：

```python
def handle_text(msg, user_ctx, db):
    session = load_or_create_session(user_ctx)
    record_context_window(session, "user", msg.content)

    intent_result = classify_intent(
        text=msg.content,
        role=user_ctx.role,
        history=session.llm_context_window,
        current_criteria=session.search_state.criteria,
        session_hint=build_session_hint(session),
    )

    if intent_result.intent == "command":
        return handle_command(intent_result, session)

    if pending_expired(session):
        return handle_pending_expired_or_continue(msg, intent_result, session)

    if session.active_flow == "upload_collecting":
        return route_upload_collecting(intent_result, msg, user_ctx, session, db)

    if session.active_flow == "upload_conflict":
        return route_upload_conflict(intent_result, msg, user_ctx, session, db)

    if session.active_flow == "search_active":
        return route_search_active(intent_result, msg, user_ctx, session, db)

    return route_idle(intent_result, msg, user_ctx, session, db)
```

---

## 6. LLM 输出升级

文件：`backend/app/llm/prompts.py`、`backend/app/services/intent_service.py`

在 prompt 中加入 session hint：

```text
当前系统状态：
- active_flow: upload_collecting
- 正在发布：岗位
- 已收集字段：city=北京, job_category=餐饮, salary_floor_monthly=7500
- 当前缺字段：headcount
- 用户本轮消息：2个人
```

长期让 LLM 输出更细类型：

```text
field_patch：补字段
replace：替换当前草稿或关键字段
abandon：取消/放弃
new_intent：切到新流程
chitchat：闲聊
```

Stage C 不要求 LLM 100% 决策，业务状态机仍是最终裁决者。

---

## 7. upload_collecting 规则

### 7.1 补字段

字段提取优先级：

```text
1. LLM field_patch / structured_data
2. criteria_patch
3. 规则解析
```

有效 patch：

```text
merge 到 draft.data
append raw_text_parts
重算 missing_fields
```

字段齐全：

```text
调用 upload_service 入库
after_commit=none -> active_flow=idle
after_commit=search 且有结果 -> active_flow=search_active
after_commit=search 且0命中 -> active_flow=idle
```

不论搜索是否有结果，都写 `search_state.last_criteria`。

### 7.2 cancel / abandon

```text
clear pending_upload
pending_interruption = None
active_flow = idle
```

### 7.3 chitchat

```text
保留 pending_upload
不递增 failed_patch_rounds
回复闲聊 + 当前缺字段提示
```

### 7.4 failed_patch_rounds

递增条件：

```text
1. 三层提取都拿不到有效字段。
2. 提取到字段但类型/范围非法。
```

不递增：

```text
命令、闲聊、取消、新意图、补了其他有效上传字段。
```

达到阈值：

```text
清 pending
active_flow=idle
回复已收集字段和缺失字段，引导整段重发
```

---

## 8. upload_conflict 规则

进入条件：

```text
upload_collecting 中，用户明确表达 search_* 或不同上传流程
```

状态动作：

```text
active_flow = upload_conflict
pending_interruption = 当前新意图瘦身版
回复确认问题
```

确认选项：

| 用户选择 | 行为 |
|---|---|
| 继续发布 | 回 `upload_collecting` |
| 先找工人/岗位 | 清 pending_upload，执行 `pending_interruption` |
| 取消草稿 | 清 pending_upload，回 `idle` |

不做多草稿暂存，除非后续引入 `parking_lot`。

---

## 9. search_active 规则

```text
follow_up -> merge criteria -> search
show_more -> 使用 candidate_snapshot
chitchat -> 保留 search_state，闲聊回复
new upload -> route_idle 的 upload 流程，必要时清 search snapshot
reset_search -> 清 criteria/candidate_snapshot/shown_items，不清 pending_upload
```

搜索无结果：

```text
candidate_snapshot=None
active_flow=idle
last_criteria 保留
```

---

## 10. 必测用例

单测和集成测试至少覆盖：

| Case | 期望 |
|---|---|
| 旧 session 反序列化 | 默认 active_flow=idle，不崩 |
| Stage A pending dict 迁移 | 可迁移或安全清理 |
| upload_collecting 补 headcount | 入库成功 |
| upload_collecting 闲聊 | 不耗 failed_patch_rounds |
| upload_collecting cancel | 清 pending |
| upload_collecting timeout | 清 pending 或提示整段重发 |
| upload_collecting 新搜索意图 | 进入 upload_conflict |
| upload_conflict 继续发布 | 回 upload_collecting |
| upload_conflict 先找工人 | 清草稿并执行 pending_interruption |
| upload_and_search 有结果 | 入库 + search_active |
| upload_and_search 0 命中 | 入库 + idle + last_criteria 保留 |
| search_active show_more | 使用 snapshot |
| /重新找 during upload_collecting | 清 search，不清 pending |
| broker /找岗位 during upload_collecting | Stage C 进入 upload_conflict |
| attach_image during upload_collecting | 仍能挂载 |

建议运行：

```powershell
cd backend
pytest tests/unit/test_conversation_service.py tests/unit/test_message_router.py tests/unit/test_upload_service.py tests/unit/test_search_service.py
pytest tests/integration/test_phase3_upload_and_search.py
```

---

## 11. 手测验收

### 场景 1：完整补字段

```text
factory：北京饭店招聘厨师，底薪7500+绩效，包吃不包住
bot：还需要补充招聘人数
factory：2个人
```

通过标准：

```text
active_flow: idle
pending_upload: None
岗位入库
raw_text 包含两轮用户消息
```

### 场景 2：切流程确认

```text
factory：北京饭店招聘厨师，底薪7500+绩效，包吃不包住
bot：还需要补充招聘人数
factory：我先看看有没有厨师简历
bot：询问继续发布/先找工人/取消草稿
factory：先找工人
```

通过标准：

```text
草稿被明确丢弃
执行搜索
不会静默吞掉上一轮草稿
```

### 场景 3：upload_and_search 0 命中

```text
factory：北京饭店招厨师5人月薪8000，顺便找人
```

通过标准：

```text
岗位入库
回复“暂未找到匹配”
active_flow=idle
last_criteria 保留
```

---

## 12. 验收标准

1. `active_flow` 成为路由裁决依据，`last_intent` 不参与路由。
2. 上传和搜索的 follow_up 分支完全分离。
3. pending upload 生命周期完整：创建、补字段、取消、超时、max rounds、完成。
4. `upload_conflict` 能处理上传中切换流程。
5. `upload_and_search` 支持有结果和 0 命中两条路径。
6. `search_state.last_criteria` 在有结果和 0 命中时都写入。
7. `attach_image` 不再依赖 `current_intent`。
8. `history` 语义收敛为 `llm_context_window`，完整审计仍使用 `conversation_log`。
9. Stage A/B 的核心测试不回退。

