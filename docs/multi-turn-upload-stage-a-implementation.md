# 多轮上传 Stage A 开发实施与验收说明

> 面向开发分发。目标是在不重构完整状态机的前提下，修复“岗位发布缺字段后，用户补人数却触发求职者推荐”的演示阻塞问题。

---

## 1. 目标

修复以下链路：

```text
用户：北京饭店招聘厨师，底薪7500+绩效，包吃不包住
系统：还需要补充招聘人数
用户：2个人
期望：岗位入库，回复“已入库”
禁止：走 search_workers 并推荐求职者
```

Stage A 只做演示阻塞修复，不引入完整 `active_flow + UploadDraft + SearchState` 状态机。

---

## 2. 改动范围

只改后端主链路：

```text
backend/app/schemas/conversation.py
backend/app/services/upload_service.py
backend/app/services/message_router.py
backend/app/services/search_service.py
backend/tests/unit/
```

不改 mock-testbed，不做完整 `upload_conflict`，不做 `failed_patch_rounds` 精细计数，不做 LLM prompt 重构。

---

## 3. 实施步骤

### 3.1 SessionState 增加过渡字段

文件：`backend/app/schemas/conversation.py`

在 `SessionState` 增加字段，必须有默认值，保证旧 Redis session 反序列化不崩：

```python
pending_upload: dict = Field(default_factory=dict)
pending_upload_intent: str | None = None
awaiting_field: str | None = None
pending_started_at: str | None = None
pending_updated_at: str | None = None
pending_expires_at: str | None = None
pending_raw_text_parts: list[str] = Field(default_factory=list)
```

时间字段使用 ISO 8601 UTC 字符串。比较时必须 `datetime.fromisoformat(...)`，不要做字符串比较。

### 3.2 process_upload 缺字段时保存 pending

文件：`backend/app/services/upload_service.py`

在 `process_upload` 的 missing 分支：

1. 将已抽取字段 merge 到 `session.pending_upload`。
2. 写入 `session.pending_upload_intent = intent_result.intent`。
3. 写入 `session.awaiting_field = missing[0]`。
4. 初始化 `pending_started_at/pending_updated_at/pending_expires_at`，默认 10 分钟过期。
5. 将首轮用户原文加入 `pending_raw_text_parts`。
6. 原有 `follow_up_rounds` 逻辑保留。

成功入库后：

1. 清空 pending 字段。
2. 重置 `follow_up_rounds`。

超过 `MAX_FOLLOW_UP_ROUNDS=2` 后：

1. 清空 pending 字段。
2. 回复整段重发提示。

### 3.3 message_router pending 守卫

文件：`backend/app/services/message_router.py`

在 `_handle_text` 中，`classify_intent(...)` 之后、覆盖 `session.current_intent` 之前，增加 pending upload 守卫。

顺序要求：

```text
1. classify_intent
2. command 优先
3. pending timeout 检查
4. cancel 检查
5. pending_upload 字段补全
6. 再写 session.current_intent 并走原 dispatch
```

字段补全逻辑：

```text
取值优先级：
1. intent_result.structured_data[awaiting_field]
2. intent_result.criteria_patch 中 field == awaiting_field 的 value
3. 正则解析，例如“2个人”“招2人”“7500”
```

如果补到有效字段：

1. merge 到 `session.pending_upload`。
2. 将本轮用户原文追加到 `pending_raw_text_parts`。
3. 构造 `IntentResult(intent=session.pending_upload_intent, structured_data=session.pending_upload)`。
4. 调用 `upload_service.process_upload(...)`。
5. `raw_text` 必须传 `"\n".join(session.pending_raw_text_parts)`，不要传最后一句 `msg.content`。

如果没补到字段：

1. 不搜索。
2. 回复“请告诉我具体的招聘人数/字段名”。

### 3.4 cancel / timeout / max rounds

文件：`backend/app/services/message_router.py`、`backend/app/services/upload_service.py`

cancel 阶段 A 只做强规则：

```text
完整句：取消 / 不发了 / 算了 / 先不发了 / 不要了
句首：不发 / 先不 / 算了，
```

timeout：

```text
pending_expires_at < now
  -> 清 pending
  -> 如果当前消息像字段补丁，回复“上次岗位草稿已超时，请整段重新发送岗位信息。”
  -> 否则继续正常 intent 分发
```

max rounds：

```text
沿用现有 MAX_FOLLOW_UP_ROUNDS=2 和 follow_up_rounds。
阶段 C 再替换为 failed_patch_rounds。
```

### 3.5 搜索安全护栏

文件：`backend/app/services/search_service.py`

在 `_query_jobs` 和 `_query_resumes` 查询前加最小条件守卫：

```python
def _has_effective_search_criteria(criteria: dict) -> bool:
    return bool(criteria.get("city") or criteria.get("job_category"))
```

无有效条件时直接返回 `[]`，禁止全表召回。

Stage A 不在 `_query_*` 内读取用户最近简历或补默认条件；后续默认条件补齐应放在 `message_router._run_search` 或独立 orchestrator。

---

## 4. 必测用例

单测至少覆盖：

| Case | 期望 |
|---|---|
| `SessionState` 旧数据缺新字段也能构造 | 不报错，默认值正确 |
| `process_upload` missing 分支 | 保存 pending、awaiting_field、raw_text_parts |
| “招厨师7500” -> “2个人” | 入库成功，不调用 search_workers |
| “招厨师7500” -> “/帮助” -> “2个人” | `/帮助` 不清 pending，后续仍可补字段 |
| pending 中 “取消” | 清 pending，回 idle/普通状态 |
| pending 超时后 “2个人” | 提示整段重发，不搜索 |
| criteria 只有 `{headcount: 2}` | `_query_resumes/_query_jobs` 返回空 |
| raw_text 拼接 | 入库 raw_text 包含首轮岗位文本和补字段文本 |

建议运行：

```powershell
cd backend
pytest tests/unit/test_upload_service.py tests/unit/test_message_router.py tests/unit/test_search_service.py
```

---

## 5. 手测验收

### 场景 1：核心阻塞 bug

```text
factory：北京饭店招聘厨师，底薪7500+绩效，包吃不包住
bot：还需要补充招聘人数
factory：2个人
```

通过标准：

```text
回复岗位已入库
不推荐求职者
数据库 Job 有完整 city/job_category/salary/headcount
raw_text 包含两轮用户文本
```

### 场景 2：命令打断

```text
factory：北京饭店招聘厨师，底薪7500+绩效，包吃不包住
bot：还需要补充招聘人数
factory：/帮助
bot：返回帮助
factory：2个人
```

通过标准：

```text
/帮助 不清 pending
最后仍能入库
```

### 场景 3：取消

```text
factory：北京饭店招聘厨师，底薪7500+绩效，包吃不包住
bot：还需要补充招聘人数
factory：取消
```

通过标准：

```text
pending_upload 清空
不会入库
不会搜索
```

### 场景 4：搜索防全表

构造搜索 criteria：

```python
{"headcount": 2}
```

通过标准：

```text
_query_jobs/_query_resumes 返回 []
不会召回全表 50 条
```

---

## 6. 验收标准

1. 核心两轮发布场景能入库。
2. pending 补字段期间不会调用 `search_workers/search_jobs`。
3. pending 超时、取消、max rounds 都会清理状态。
4. 空条件或只有 `headcount` 的搜索不会全表召回。
5. `/帮助` 不清 pending，用户回来仍可补字段。
6. 入库 `raw_text` 不只包含最后一句。
7. 现有图片附件逻辑不受影响，Stage A 保留 `current_intent`。

