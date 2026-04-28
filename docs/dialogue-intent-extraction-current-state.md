# 多轮会话意图识别与信息抽取：现状与优化方向

日期：2026-04-28

## 1. 背景

近期 mock 对话暴露出一个典型问题：

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

从用户语义看，三轮分别应该是：

1. 发起找岗位：`city=西安市`，`job_category=餐饮`
2. 回答上一轮追问或补充薪资：`salary_floor_monthly=2500`
3. 修改已有搜索条件：把 `city` 从 `西安市` 替换为 `北京市`，并保留 `job_category=餐饮`、`salary_floor_monthly=2500`

当前表现说明：系统已经有多轮 session，但“意图识别 + 槽位抽取 + 条件合并”还不是一个完整的对话理解层，仍然较依赖 LLM 单轮输出。

## 2. 当前链路现状

### 2.1 主流程

当前文本消息的主链路在 [message_router.py](../backend/app/services/message_router.py)：

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

`classify_intent` 调用时会传入：

- 当前用户文本 `text`
- 用户角色 `role`
- 最近对话历史 `history`
- 当前搜索条件 `current_criteria=session.search_criteria`
- `session_hint`（当前只用于日志记录，provider 暂不消费）

### 2.2 当前 LLM 输出结构

当前意图抽取结果定义在 [base.py](../backend/app/llm/base.py)：

```json
{
  "intent": "search_job | search_worker | follow_up | upload_job | upload_resume | ...",
  "structured_data": {},
  "criteria_patch": [],
  "missing_fields": [],
  "confidence": 0.0
}
```

这个结构更像“业务路由结果”，而不是“对话行为理解结果”。它把以下几件事压在同一个 `intent` 里：

- 用户是在发起新流程，还是继续旧流程
- 是补充上一轮缺失字段，还是修改搜索条件
- 是搜索岗位、找工人、发布岗位、上传简历
- 当前字段应该替换、追加、删除，还是只作为补充

### 2.3 当前已有能力

目前并不是完全无状态，已有一些重要基础：

- `SessionState` 已保存 `history`、`search_criteria`、`candidate_snapshot`、`last_criteria`、`active_flow`、`pending_upload`、`awaiting_field` 等上下文。
- `active_flow` 已能区分 `idle / search_active / upload_collecting / upload_conflict`。
- prompt 已包含 `history` 和 `current_criteria`，并要求 `follow_up` 输出完整 criteria 快照。
- `intent_service` 已做字段清洗、工种同义词归并、数值范围检查、部分搜索字段重映射。
- `_handle_search` 会把本轮 `structured_data` 合入 `session.search_criteria`。
- `_handle_follow_up` 已支持用完整 `structured_data` 替换 `session.search_criteria`，降低 `criteria_patch add/update` 歧义。
- `_compute_search_missing` 会过滤 session 中已存在的字段，避免重复追问。

这些能力说明：会话状态的底座已经有了，但“如何解释一句话在当前状态中的作用”还不够体系化。

## 3. 当前主要问题

### 3.1 intent 混合了“业务目标”和“对话行为”

例如 `search_job` 同时可能表示：

- 第一次发起岗位搜索
- 在已有搜索中补充条件
- 在已有搜索中修改条件
- 回答上一轮追问

`follow_up` 又同时可能表示：

- 修改城市
- 补充薪资
- 改工种
- 放宽条件
- 继续当前搜索

这会导致后端难以稳定判断“本轮应该继承哪些旧条件、覆盖哪些条件、追问哪些字段”。

### 3.2 missing_fields 过度依赖 LLM

在样例中，用户是 worker 找工作，但系统追问了“计薪方式”“招聘人数”。这两个字段属于发布岗位必填字段，不应该出现在 worker 搜索岗位的追问里。

说明当前 `missing_fields` 虽有清洗，但没有严格按“角色 + 当前 frame + 当前 act”重新计算。

更合理的方式是：

- LLM 可以提示可能缺什么；
- 后端必须基于业务 schema 重新计算最终 `missing_fields`；
- 搜索场景和上传场景的必填字段要分开；
- worker / factory / broker 的可追问字段要分开。

### 3.3 缺少显式 merge_policy

“北京有吗”“换成北京”“北京也行”都包含城市，但合并语义不同：

- “北京有吗”通常是替换当前城市
- “换成北京”明确替换
- “北京也行”明确追加

当前结构里，字段值和合并策略没有分离。过去依赖 `criteria_patch.add/update`，后来改成 follow_up 输出完整快照，但本质仍靠 LLM 一次性给对。

更稳的结构应该显式表达：

```json
{
  "slots_delta": {"city": ["北京市"]},
  "merge_policy": {"city": "replace"}
}
```

### 3.4 短句缺少上下文补全层

用户在多轮对话中经常只发短句：

- `2500`
- `北京有吗`
- `饭店服务员`
- `包住`
- `高一点`

这些短句离开上下文几乎不可解释，但结合 `active_flow`、`awaiting_field`、`search_criteria` 后很明确。

当前系统虽然把 history/current_criteria 给了 LLM，但缺少确定性的短句补全策略：一旦 LLM 漏抽字段，就会沿用旧条件或走错分支。

### 3.5 session_hint 已构造但未进入 prompt

`intent_service.build_session_hint(session)` 已能构造：

- `active_flow`
- `awaiting_field`
- `pending_upload_intent`
- `pending_upload`

但当前 provider 暂不消费它，只记录日志。这意味着 LLM 实际看不到完整状态机信息，尤其是“当前正在追问哪个字段”。

### 3.6 测试更偏“LLM 已抽对”的路径

已有单测覆盖了不少后端合并和过滤逻辑，但样例里的真实风险是：

- LLM 抽错 intent
- LLM 漏抽城市
- LLM 给了错误 missing_fields
- LLM 未按上下文输出完整快照

这些需要用 golden conversation 方式覆盖，而不是只 mock 一个理想 IntentResult。

## 4. 建议目标架构

建议把当前 `intent_service` 演进成一个更通用的“对话理解层”（Dialogue Understanding Layer），输出从“业务路由 intent”升级为“对话行为 + 业务 frame + 槽位 delta + 合并策略”。

### 4.1 推荐输出结构

```json
{
  "dialogue_act": "start_search | modify_search | answer_missing_slot | show_more | start_upload | cancel | reset | chitchat",
  "frame": "job_search | worker_search | job_upload | resume_upload | none",
  "slots_delta": {
    "city": ["北京市"],
    "job_category": ["餐饮"],
    "salary_floor_monthly": 2500
  },
  "merge_policy": {
    "city": "replace",
    "job_category": "keep",
    "salary_floor_monthly": "update"
  },
  "missing_slots": [],
  "confidence": 0.92,
  "needs_clarification": false
}
```

说明：

- `dialogue_act` 解决“这句话在对话中干什么”。
- `frame` 解决“当前业务对象是什么”。
- `slots_delta` 只表达本轮抽到的新信息。
- `merge_policy` 明确替换、追加、删除、保留。
- `missing_slots` 由后端最终重算，LLM 结果只作参考。

### 4.2 以样例为目标行为

第一轮：

```json
{
  "dialogue_act": "start_search",
  "frame": "job_search",
  "slots_delta": {
    "city": ["西安市"],
    "job_category": ["餐饮"]
  },
  "merge_policy": {
    "city": "replace",
    "job_category": "replace"
  },
  "missing_slots": []
}
```

第二轮：

```json
{
  "dialogue_act": "modify_search",
  "frame": "job_search",
  "slots_delta": {
    "salary_floor_monthly": 2500
  },
  "merge_policy": {
    "salary_floor_monthly": "update"
  },
  "missing_slots": []
}
```

第三轮：

```json
{
  "dialogue_act": "modify_search",
  "frame": "job_search",
  "slots_delta": {
    "city": ["北京市"]
  },
  "merge_policy": {
    "city": "replace"
  },
  "missing_slots": []
}
```

最终后端合并后的 criteria：

```json
{
  "city": ["北京市"],
  "job_category": ["餐饮"],
  "salary_floor_monthly": 2500
}
```

## 5. 优化路线

### 阶段一：收紧现有链路，降低误判

不大改结构，先补确定性护栏：

- 搜索场景下，后端按角色和 frame 重算 `missing_fields`，不要直接信 LLM。
- worker 场景中，明显“找工作/找岗位”的表达不得被路由成 `upload_job`。
- `answer_missing_slot` 型短句先看 `awaiting_field`，例如 `2500` 优先解释为上一轮追问字段。
- 对城市、薪资、工种、住宿等高频短句增加轻量抽取兜底。
- provider 消费 `session_hint`，让 LLM 明确知道当前 `active_flow` 和 `awaiting_field`。

这一阶段目标是先让当前事故样例稳定正确。

### 阶段二：引入 frame + dialogue_act

新增中间 DTO，例如 `DialogueUnderstandingResult`，让 LLM 输出 `dialogue_act/frame/slots_delta/merge_policy`。

同时保留兼容层，把新 DTO 转换成旧的 `IntentResult`，降低一次性迁移风险。

建议先覆盖四个 frame：

- `job_search`：工人找岗位
- `worker_search`：厂家/中介找工人
- `job_upload`：发布岗位
- `resume_upload`：上传简历

### 阶段三：建立统一 Slot Schema

用一份 schema 描述每个 frame 的字段：

- 字段名
- 类型
- 是否必填
- 是否可追问
- 默认 merge policy
- 同义词/归一化规则
- 角色权限

后端所有 `missing_slots`、字段清洗、追问文案、合并策略都从 schema 派生，避免 prompt 和业务代码各维护一套。

### 阶段四：LLM + 规则双层抽取

建议把抽取拆成两类：

确定性抽取：

- 城市字典
- 薪资数字
- 工种词典
- 包吃/包住
- 更多/取消/重置

LLM 抽取：

- 复杂自然语言意图
- 放宽/收紧条件
- 替换/追加语义
- 多字段组合表达

最终由后端 reducer 合并，而不是让 LLM 直接改 session。

### 阶段五：golden conversation 评测

建立一组多轮对话样例，每条断言：

- 每轮 `dialogue_act`
- 每轮 `frame`
- 每轮 `slots_delta`
- 每轮 merge 后的 `session.search_criteria`
- 是否追问
- 最终调用 `search_jobs` / `search_workers` 的 criteria

建议首批覆盖：

- 城市替换：`北京有吗`、`换成苏州`
- 城市追加：`苏州也行`
- 数字补槽：`2500`、`6000以上`
- 工种改写：`饭店服务员`、`打包分拣`
- 福利补充：`要包住的`
- 放宽条件：`薪资低点也行`、`不限住宿`
- 角色边界：worker 不发布岗位，factory 不找岗位

## 6. 结论

当前系统已经具备多轮会话状态机和 session 存储基础，但“意图识别与信息抽取”仍主要是 LLM 直接输出业务 intent，后端只做有限清洗和合并。

下一步更值得做的是把它抽象为通用对话理解层：

```text
用户文本 + 当前状态
  -> dialogue_act + frame + slots_delta + merge_policy
  -> 后端 schema 校验、missing 重算、状态合并
  -> 路由到搜索/上传/追问
```

这样可以把“让 LLM 猜完整业务状态”改成“让 LLM 只理解语言，后端负责业务一致性”，会更稳定，也更容易扩展到后续更多招聘对话场景。
