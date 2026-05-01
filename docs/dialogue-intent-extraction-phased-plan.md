# 多轮会话意图识别与信息抽取：分阶段实施说明

日期：2026-04-30
基线提交：`fdeb18d`
配套文档：[dialogue-intent-extraction-current-state.md](dialogue-intent-extraction-current-state.md)、[keyword-rules-audit.md](keyword-rules-audit.md)

本文是对现状文档 §8「阶段路线与退出标准」的细化，按 **功能 / 边界 / 改动范围 / 验收条件** 四要素拆解每个阶段。所有阶段必须遵守现状文档 §9 列出的非目标，特别是：LLM 不直接写 session、不引入完整 DST、不靠扩关键词表解决开放语言。

---

## 阶段一：收紧现有链路（Tighten existing pipeline）

定位：在不改 LLM 输出 schema 的前提下，把 `fdeb18d` 留下的几处 frame 误判和补槽落地缺口补齐，给后续 DTO 重构提供干净的回归基线。

### 1.1 功能

1. **worker 搜索信号护栏**：worker 角色 + “找/想找/求职/工作/打工/上班”等词，且不带明确发布意图（“我招/招聘/发布/发岗位”）时，强制 frame 落到 `job_search`，不再走 `upload_job` 错误分支。
2. **搜索流程 awaiting 物化**：`_handle_search` 因缺字段追问时，把当轮缺失字段写入 session（FIFO 队列 + 过期时间 + 所属 frame）。下一轮裸值优先按字段类型落槽。
3. **search 场景 missing 后端重算**：frame 校正后，由后端**临时 legacy schema**（见下文 §1.3.bis）重算 `missing_fields`，不再直接信任 LLM 输出的 missing。这里的「schema」**不是阶段三的统一 slot schema**，而是基于现有常量包一层 helper：搜索 frame **复用** [prompts.py](../backend/app/llm/prompts.py) 的 `SEARCH_JOB_MIN_FIELDS / SEARCH_WORKER_MIN_FIELDS` 的**语义**（`SEARCH_WORKER_MIN_FIELDS` 是「any-of」语义，要通过 helper 表达成 `required_any`，不能直接拿 frozenset 当 required_all）；上传 frame 复用 [intent_service.py](../backend/app/services/intent_service.py) 的 `_VALID_JOB_KEYS / _VALID_RESUME_KEYS`。阶段一不新增 schema 文件。
4. **`session_hint` 真正注入 prompt**：provider 把 `active_flow / awaiting_field / search_criteria 摘要 / pending_upload 摘要` 拼入 system 或 user prompt（结构化注入，不要拼成长篇自然语言），让 LLM 看到当前会话状态。
5. **回归 golden case**：worker「西安饭店服务员 → 2500 → 北京有吗」、broker「机械厂普工 → 北京 → 换成苏州」固化为单测，长期防回退。**含「北京有吗」歧义的 case 必须在 fixture 顶层显式声明 `ambiguous_city_query_policy`**（阶段一 fixture 用 `replace`，阶段二在 dual-read 路径下复制一份新 fixture 用 `clarify`），避免同一个 case 在两个阶段间预期漂移；断言策略由 fixture 中声明的策略驱动，而不是隐式跟随阶段。

### 1.2 边界（什么不做）

- **不**改 [base.py](../backend/app/llm/base.py) 的 `IntentResult` 结构。
- **不**新增 `DialogueParseResult` / `DialogueDecision`。
- **不**移除 [intent_service.py](../backend/app/services/intent_service.py) 中 `_WORKER_SEARCH_SIGNALS / _JOB_POSTING_SIGNALS / _CITY_ADD_SIGNALS / _CITY_REPLACE_SIGNALS` —— 阶段一只实际接入 worker/job posting 两组护栏；`_CITY_*` 两组仅保留声明，具体边界见下文「北京有吗」说明。
- **不**统一 slot schema、**不**引入 reducer 层。
- **不**做灰度切流：阶段一所有改动直接在主链路生效，没有 shadow / dual-read。
- 「北京有吗」歧义在阶段一**临时保留** `replace` 行为。**校正：当前 `_CITY_REPLACE_SIGNALS` / `_CITY_ADD_SIGNALS` 这两个常量在 `intent_service.py` 第 209-210 行只是声明，全仓库没有任何调用点。所以「沿用现有信号」是误读。当前 replace 行为实际来自两步：(1) LLM 在 prompt 约束下把「北京有吗」识别为 follow_up + 输出仅含新城市的完整 criteria 快照；(2) [_handle_follow_up](../backend/app/services/message_router.py) 调用 `replace_criteria(session, full_criteria)` 物化。**

  阶段一处理：
  - **不**新接入 `_CITY_REPLACE_SIGNALS` / `_CITY_ADD_SIGNALS`（既然原本就是死代码，阶段一也不要让它们活过来）。
  - 阶段一对「北京有吗」的 replace 行为 = **维持 LLM 全量快照 + `replace_criteria` 路径不变**，不引入额外关键词分支。`_CITY_REPLACE_SIGNALS` / `_CITY_ADD_SIGNALS` 在阶段一保留为死常量（不删，但也不接入），便于阶段二判断要不要直接删除。
  - 这是兼容策略，**不代表最终语义**；现状文档 §3.4 的默认建议仍是 `clarify`。
  - 最终策略由阶段二通过配置项 `ambiguous_city_query_policy ∈ {clarify, replace}` 切换，默认值在阶段二上线时由产品确认（建议 `clarify`）。
  - 阶段一**不在文档或代码中把 replace 写成长期决策**。

### 1.3 改动范围（具体文件 / 函数）

| 文件 | 改动点 |
|---|---|
| [backend/app/services/intent_service.py](../backend/app/services/intent_service.py) | `classify_intent` 增加 worker 搜索护栏（`role=worker` + `_WORKER_SEARCH_SIGNALS` 命中且无 `_JOB_POSTING_SIGNALS` 时，强制 `intent=search_job`）；`_sanitize_intent_result` 校正后兜底；`build_session_hint` 增补 `awaiting_fields / awaiting_frame / awaiting_expires_at` 字段。 |
| [backend/app/schemas/conversation.py](../backend/app/schemas/conversation.py) | `SessionState` 新增三个字段（带默认值，旧 Redis session 兼容），按现有 Pydantic 风格用 `Field(default_factory=...)`：`awaiting_fields: list[str] = Field(default_factory=list)`、`awaiting_frame: str \| None = Field(default=None)`、`awaiting_expires_at: str \| None = Field(default=None)`。**不**复用上传的 `awaiting_field`，避免与上传草稿混淆。 |
| [backend/app/services/conversation_service.py](../backend/app/services/conversation_service.py) | 新增 `set_search_awaiting(session, fields, frame, ttl_seconds)` / `consume_search_awaiting(session, slots_delta)` / `clear_search_awaiting(session)`；TTL 默认复用上传草稿 TTL（10 分钟），通过配置项 `search_awaiting_ttl_seconds` 单独可调。 |
| [backend/app/services/message_router.py](../backend/app/services/message_router.py) | `_handle_search` 计算 missing 后写入 awaiting；`_handle_follow_up` 命中 awaiting 字段后调用 `consume_search_awaiting`；搜索成功 / `cancel` / 进入上传时 `clear_search_awaiting`。 |
| [backend/app/llm/base.py](../backend/app/llm/base.py) | **接口变更**：`IntentExtractor.extract(...)` 抽象方法签名新增 `session_hint: dict \| None = None` 参数（带默认值，向后兼容）。`IntentResult` 结构本身不动。 |
| [backend/app/llm/prompts.py](../backend/app/llm/prompts.py) | system prompt 中加入「当前会话状态片段」模板（结构化键值，不要拼成长篇自然语言），把 `session_hint` 字典渲染进去；保留现有 few-shot。 |
| `backend/app/llm/` 各 provider 实现 | **签名同步**：所有现存 `IntentExtractor` 子类的 `extract(...)` 必须同步新增 `session_hint` 形参；至少一个具体 provider 真正读取并拼入 prompt，其它 provider 至少接收形参不报错（避免 mock provider 漏改）。`classify_intent` 调用 provider 时把 `build_session_hint(session)` 透传下去。 |
| [backend/tests/fixtures/dialogue_golden/](../backend/tests/) | 新建目录及 YAML loader / `run_dialogue_case` 断言工具（按现状文档 §7 的格式预留位）。阶段一只录两条 happy path golden + 一条 broker case。 |
| [backend/tests/](../backend/tests/) | 新增/扩展单测：worker 误判修正、worker 搜索 awaiting 中裸值 `2500 → salary_floor_monthly`、`job_upload` 草稿 awaiting 中裸值 `2 → headcount`（两条独立用例，验证 awaiting 不跨 frame）、awaiting 过期、城市归一。 |

### 1.3.bis 临时 legacy schema 约定

阶段一/阶段二**不新建** schema 文件。所有「按 frame schema 校验 / 重算 missing」的引用都指向以下既有常量，包一层 helper 函数即可：

| frame | required_all | required_any（任一即可） | 合法字段集合 |
|---|---|---|---|
| `job_search` | `{city, job_category}`（来自 [prompts.py](../backend/app/llm/prompts.py) `SEARCH_JOB_MIN_FIELDS`） | — | **当前 [search_service](../backend/app/services/search_service.py) `_query_jobs` 真正消费的集合**：`city / job_category / salary_floor_monthly / is_long_term / gender_required / age`（其中 `gender_required` 受 `filter.enable_gender` 配置门控、`age` 受 `filter.enable_age` 门控，配置关闭时**抽出但不过滤**）。`_VALID_JOB_KEYS` 中其它字段（`headcount / pay_type / dorm_condition / shift_pattern / provide_meal / provide_housing / accept_couple / education_required / experience_required / district / salary_ceiling_monthly / rebate / employment_type / contract_type / min_duration / job_sub_category / age_min / age_max / accept_student / accept_minority / height_required` 等）**抽出后不参与硬过滤**——见下文「可抽出但不检索字段」表 |
| `candidate_search` | `{}` | `{city, job_category}`（**复用** [prompts.py](../backend/app/llm/prompts.py) `SEARCH_WORKER_MIN_FIELDS` 的**语义**「city OR job_category 至少 1 个」，但**不直接拿其 frozenset 当 required_all 用**——通过 helper `_legacy_required(frame)` 返回 `(required_all, required_any)` 表达） | **搜索 criteria key，限定为当前 [search_service](../backend/app/services/search_service.py) `_query_resumes` 真正消费的集合**：`city / job_category / salary_ceiling_monthly / gender / age`。**不**含 `expected_cities / expected_job_categories / salary_expect_floor_monthly` 等 resume DB 字段——这些只属于 `resume_upload`。`age_min / age_max / education / experience_required / district / salary_floor_monthly` 等字段等 `_query_resumes` 后续支持后再加入合法集；阶段一/二抽出来检索层也不消费，**避免「抽出但搜索不生效」的假象** |
| `job_upload` | 由 [upload_service](../backend/app/services/upload_service.py) 现有必填校验决定 | — | `_VALID_JOB_KEYS` |
| `resume_upload` | 由 upload_service 现有必填校验决定 | — | `_VALID_RESUME_KEYS`（含 `expected_*`） |

关于 `candidate_search` 的字段集合，必须严格区分**搜索 criteria** 和 **resume DB 字段**：

- 当前 [search_service](../backend/app/services/search_service.py) `_query_resumes` 读 `criteria["city"] / criteria["job_category"]`，**不**读 `expected_cities / expected_job_categories`。
- prompt 已明确「搜索 / follow_up 一律用 `city / job_category`，即便 broker 在找工人」，对应 `_SEARCH_FIELD_REMAP` 把 `expected_*` 重映射回 `city / job_category`。
- 如果把 `_VALID_RESUME_KEYS` 直接当作 `candidate_search` 合法字段集，会让 reducer 接受 LLM 输出的 `expected_cities`，把已经修过的 Bug A（`fdeb18d` 的搜索字段归一）请回来。
- `_SEARCH_FIELD_REMAP` 只作为**兼容兜底**：reducer 接到 `expected_*` 时先 remap 到 `city / job_category` 再校验，不写进 `candidate_search` 的合法主 schema。

required_all / required_any 不能塞进一个 `frozenset`。helper 建议放在 [backend/app/services/intent_service.py](../backend/app/services/intent_service.py)：

```python
def _legacy_required(frame: str) -> tuple[frozenset[str], frozenset[str]]:
    """返回 (required_all, required_any)。required_any 任一命中即满足。"""

def _legacy_valid_fields(frame: str) -> frozenset[str]:
    ...

def _legacy_compute_missing(frame: str, criteria: dict) -> list[str]:
    """required_all 全部缺失才算 missing；required_any 整组缺失时给一个组合占位。"""
```

阶段三用 schema 替换时只换 helper 内部实现，调用方不动。

### 1.3.ter 「可抽出但不检索」字段说明（避免 silently dropped）

LLM 可以从用户文本里抽出一批字段、`_VALID_JOB_KEYS / _VALID_RESUME_KEYS` 中也存在，但当前 [search_service](../backend/app/services/search_service.py) 的 `_query_jobs / _query_resumes` 并**不**对这些字段做硬过滤。这类字段的现状必须显式登记，避免开发者抽出后误以为搜索会生效，或在 golden case 里写出错误期望。

| 字段 | LLM 抽出？ | 写入 `search_criteria`？ | 真正参与 SQL 过滤？ | 处理方式 |
|---|---|---|---|---|
| `provide_meal`（包吃） | 是 | 是（Stage B 抽取保留） | **否** | 阶段一/二仅作为 `search_criteria` 元数据保留；不进 `_legacy_required(job_search).required_*`；不参与 missing 重算；reducer 接受但不期待召回变化 |
| `provide_housing`（包住） | 是 | 是 | **否** | 同上 |
| `dorm_condition` / `shift_pattern` / `work_hours` | 是 | 是 | **否** | 同上 |
| `pay_type` | 是 | 是 | **否** | 同上；上传草稿场景下是必填字段，搜索场景下不参与过滤 |
| `accept_couple` / `accept_student` / `accept_minority` | 是 | 是 | **否** | 同上 |
| `education_required` / `experience_required` / `height_required` | 是 | 是 | **否** | 同上 |
| `district` | 是 | 是 | **否** | 同上；当前只按 `city` 过滤 |
| `salary_ceiling_monthly`（job_search 视角） | 是 | 是 | **否**（jobs 表只按 `salary_floor_monthly` 过滤） | 同上。注意：`candidate_search` 视角下 `salary_ceiling_monthly` 是**真正参与过滤**的字段（resumes 表读 `Resume.salary_expect_floor_monthly <= salary_ceiling`），两个 frame 含义不同 |
| `rebate / employment_type / contract_type / min_duration / job_sub_category` | 是 | 是 | **否** | 同上 |

阶段一/二的硬规则：

1. reducer 在 `validate_slots_delta` / `accepted_slots_delta` 中**接受**这些字段（不 drop），写入 `final_search_criteria`，便于日志归因和未来 search_service 升级时复用；
2. **`_legacy_required(frame)` 不把这些字段列为 required**，`_legacy_compute_missing` 不会针对它们追问；
3. **golden case 不能断言**「抽出 `provide_meal=true` 后召回结果数变化」，否则会写出永远跑不绿或行为依赖未实现 SQL 的脆弱断言；
4. 阶段三 schema 中这些字段标记 `filter_mode=soft` 或 `filter_mode=display`，`ranking_weight=None`，由 schema 渲染 prompt 时仍可让 LLM 抽，但 reducer 不算 missing、search_service 不做硬过滤；
5. 后续 Phase 5 / search_service 升级新增软偏好排序或 SQL 过滤后，只调整对应字段的 `filter_mode / ranking_weight`，无需改 reducer 主流程。

### 1.4 裸值消费策略（搜索 awaiting）

阶段一明确：`answer_missing_slot` 不机械按队首消费。

- 候选字段集合**仅来自当前 frame 的搜索可追问字段**，不能跨 frame 消费：
  - `job_search`（worker 找工作）：候选 `salary_floor_monthly`（lo=500, hi=200000）等搜索字段；**不**消费 `headcount`（这是岗位上传字段，worker 搜索流程不应出现）。
  - `candidate_search`（broker / factory 找人）：候选字段以「找人」搜索可追问字段为准。
  - `job_upload`：上传草稿的 awaiting 与搜索 awaiting **互不复用**；`headcount` 只在 `job_upload` 的 awaiting 队列里出现。
- 优先按字段类型 + 取值范围匹配（复用 / 对齐 `intent_service._normalize_int_field` 的 lo/hi）：例如 `awaiting_fields=[salary_floor_monthly]` 时，裸值 `2500` 命中薪资。
- LLM 已抽出的 `slots_delta` 优先；只有当本轮 LLM 没抽到字段、或抽到的字段非法时，才用 awaiting tie-break 裸值。
- 每条字段只消费一次；消费成功后从队列移除；queue 空则清 awaiting。

### 1.4.bis Worker golden case 逐轮预期（规范）

「西安饭店服务员 → 2500 → 北京有吗」case 在阶段一作为 fixture（路径 `backend/tests/fixtures/dialogue_golden/worker_xian_to_beijing_replace.yaml`）落地，**逐轮断言如下**。这里写实是为避免开发与测试各按各的理解写预期。

**初始 session：**`role=worker`、`active_flow=idle`、`search_criteria={}`、`awaiting_fields=[]`、`pending_upload={}`、`pending_upload_intent=None`。

**Turn 1：用户输入 `西安，想找个饭店的服务员的工作`**

期望：

| 字段 | 期望值 | 说明 |
|---|---|---|
| 阶段一 LLM `intent` | `search_job` | worker 搜索护栏起作用，不再被错判为 `upload_job`（断言 `intent != upload_job`） |
| 阶段二 `dialogue_act` | `start_search` | |
| 阶段二 `resolved_frame` | `job_search` | |
| `accepted_slots_delta` | `{city: ["西安市"], job_category: ["餐饮"]}` | 城市归一化后是 `西安市` 不是 `西安`；`饭店服务员` 归类为 `餐饮` |
| 处理后 `session.search_criteria` | `{city: ["西安市"], job_category: ["餐饮"]}` | |
| `missing_fields` / `missing_slots` | `[]` | `SEARCH_JOB_MIN_FIELDS` 已满足；`salary_floor_monthly` **不**算 missing |
| `awaiting_fields` | `[]` | `job_search` 的 required 已齐，不写 awaiting |
| `route_intent`（兼容派生） | `search_job` | |
| 应触发实际 SQL 检索 | 是 | 因为 `has_effective_search_criteria(criteria)` 真 |
| 应回复 | 检索结果 / 0 命中文案 | **不**应再追问「计薪方式 / 招聘人数 / 月薪下限」 |

**Turn 2：用户输入 `2500`（裸数值）**

主路径期望（**唯一**主断言，golden case 主体）：

| 字段 | 期望值 | 说明 |
|---|---|---|
| 阶段一 LLM `intent` | `follow_up`（**强制**） | 已存在 `search_criteria` 的情况下，prompt 必须把裸值续条件识别为 follow_up，不允许是 `search_job` |
| 阶段二 `dialogue_act` | `modify_search` | 此前若 awaiting 为空（Turn 1 没写 awaiting），LLM 应直接判为修改搜索条件 |
| 阶段二 `resolved_frame` | `job_search` | |
| `accepted_slots_delta` | `{salary_floor_monthly: 2500}` | 裸值 `2500` 落 `salary_floor_monthly`（lo=500, hi=200000 命中） |
| LLM `structured_data`（阶段一全量快照语义） | `{city: ["西安市"], job_category: ["餐饮"], salary_floor_monthly: 2500}` | follow_up 必须输出完整 criteria 快照（`fdeb18d` 既定约定） |
| 合并后 `session.search_criteria` | `{city: ["西安市"], job_category: ["餐饮"], salary_floor_monthly: 2500}` | 通过 `replace_criteria(full_criteria)` 一次性写入 |
| `route_intent`（兼容派生） | `follow_up` | 通过 [_handle_follow_up](../backend/app/services/message_router.py) 路径 |
| 实际进入的 handler | `_handle_follow_up` | 主断言要锁住 handler 入口，不只是 criteria 末态 |
| 应触发实际 SQL 检索 | 是 | |

**兼容单测（单独一条 unit test，不放进 golden 主断言）**：

如果阶段一灰度期间 LLM 偶发漂移把 Turn 2 识别为 `intent=search_job`，路由会走 `_handle_search`：

| 字段 | 期望值 |
|---|---|
| LLM `intent` | `search_job` |
| `intent_result.structured_data` | `{salary_floor_monthly: 2500}`（仅本轮抽到的字段） |
| `_handle_search` merge 后 `session.search_criteria` | `{city: ["西安市"], job_category: ["餐饮"], salary_floor_monthly: 2500}`（**不清**旧 city/job_category，依靠 [message_router.py](../backend/app/services/message_router.py) `_handle_search` 中 `{**session.search_criteria, **new_criteria}` merge 行为） |
| `route_intent` | `search_job` |
| 实际进入的 handler | `_handle_search` |

兼容单测的目的：**只验证「漂移到 search_job 时旧 city/job_category 不被清空」这一兜底属性**，不再把它写进 golden case 主断言；阶段二上线后该兼容单测仍保留，在 LLM 升级 / prompt 调整时充当回归网。

**Turn 3：用户输入 `北京有吗`**

期望（fixture 顶层声明 `ambiguous_city_query_policy=replace`，对应阶段一行为）：

| 字段 | 期望值 | 说明 |
|---|---|---|
| 阶段一 LLM `intent` | `follow_up` | |
| 阶段一 LLM `structured_data` | `{city: ["北京市"], job_category: ["餐饮"], salary_floor_monthly: 2500}` | 完整 criteria 快照，城市替换；`fdeb18d` 已修，禁止 `["西安市", "北京市"]` |
| 阶段二 `dialogue_act` | `modify_search` | |
| 阶段二 `resolved_frame` | `job_search` | |
| 阶段二 `resolved_merge_policy` | `{city: "replace"}` | 由 reducer 在 `policy=replace` 下决策；LLM `merge_hint` 此时多半是 `unknown`，不影响 |
| `final_search_criteria` | `{city: ["北京市"], job_category: ["餐饮"], salary_floor_monthly: 2500}` | |
| 处理后 `session.search_criteria` | 同上 | |
| `route_intent`（兼容派生） | `follow_up` | |
| 应触发实际 SQL 检索 | 是 | 必须真的换到北京查；**不**应再回西安结果 |
| `needs_clarification` | `false` | `policy=replace` 下不反问 |

**同样的 Turn 3 在 fixture `policy=clarify` 下（阶段二 dual-read 才生效）**：`needs_clarification=true`、`clarification.kind=city_replace_or_add`、`ambiguous_field=city`、`options=["replace","add"]`、**不触发 SQL 检索**、`session.search_criteria` 保持 Turn 2 末态（即 `city=["西安市"]` 不变，等用户澄清）。

### 1.5 验收条件

必须全部通过才视为阶段一退出：

1. 现状文档 §8.1 列出的两条 golden case（worker 西安 + broker 苏州）在 CI 上稳定通过 ≥ 50 次连跑无 flake。「北京有吗」case 在 fixture 顶层显式声明 `ambiguous_city_query_policy=replace`，断言对应该策略下的 `route_intent=follow_up` 与城市替换结果；阶段二上线 `clarify` 策略时**不修改**这份 fixture，而是新建另一份 `policy=clarify` 的 fixture 并行存在。
2. 新增单测覆盖：
   - worker 误判 `upload_job` → 强制 `search_job`（含 prompt 注入和 sanitize 兜底两条路径）；
   - 搜索追问 → 仅 `salary_floor_monthly` 在 `awaiting_fields` 中时，裸值 `2500` 落 `salary_floor_monthly`；
   - `job_upload` 草稿追问 → `awaiting_field=headcount` 时裸值 `2` 落 `headcount`；
   - 跨 frame 隔离 → 搜索 awaiting 中**不**出现 `headcount`，上传 awaiting 不被搜索路径消费；
   - awaiting 超过 TTL 后裸值不再补槽，走 chitchat / 重新搜索；
   - 搜索成功、`/取消`、`upload_job` 切流后 awaiting 被清空；
   - `session_hint` 缺字段 / 旧 Redis session 反序列化不报错。
3. [keyword-rules-audit.md](keyword-rules-audit.md) 中 HIGH 风险项有「阶段二接管计划」字段（不需要在阶段一移除）。
4. 新字段 `awaiting_fields / awaiting_frame / awaiting_expires_at` 在 `SessionState` fixture 和所有 conftest 默认值齐备，本地 pytest 全绿。
5. 灰度策略：直接全量上线（无 shadow），上线后观察 7 天，比较 `intent=upload_job` 在 worker 角色下的占比是否显著下降（埋点：`role=worker` 且 `intent=upload_job` 的请求数 / role=worker 总请求数）。

---

## 阶段二：引入 DialogueParseResult / DialogueDecision（Shadow → Dual-read）

定位：在阶段一的干净基线上，引入新双层 DTO 和 reducer，把 LLM 的语言理解和后端的状态裁决正式拆开。阶段二**不进入 primary**：shadow 模式不影响线上路由（仅写日志），dual-read 模式只对白名单 / hash 桶命中的用户切到新派生路由，未命中的用户继续走 legacy。

### 2.1 功能

1. **新 DTO 双层落地**：
   - LLM 解析层 `DialogueParseResult`：`dialogue_act / frame_hint / slots_delta / merge_hint / needs_clarification / confidence / conflict_action`。其中 `conflict_action` 仅在 `dialogue_act=resolve_conflict` 时出现（详见 §2.1.8），其它情况为 `None`。
   - 后端裁决层 `DialogueDecision`：`dialogue_act / resolved_frame / accepted_slots_delta / resolved_merge_policy / final_search_criteria / missing_slots / route_intent / clarification / state_transition / pending_interruption / awaiting_ops / post_search_action`。其中 `post_search_action` 仅作为 Phase 5 结果感知策略的兼容预留位，阶段二到阶段四固定为 `none`，不参与路由。
2. **reducer 实现**：**纯函数** `reduce(parse_result, session, role) -> DialogueDecision`，输入只读、输出 DTO，**不写 session、不调 handler、不调 LLM**。需要的状态变更全部以声明式字段表达：
   - `DialogueDecision.state_transition: Literal['none','enter_upload_conflict','exit_upload_conflict','enter_search_active','reset_search','clear_awaiting',...]`；
   - `DialogueDecision.pending_interruption: dict | None`（仅当 `state_transition=enter_upload_conflict` 时填）；
   - `DialogueDecision.awaiting_ops: list`（清空 / 写入搜索 awaiting 的指令）。
   - `DialogueDecision.post_search_action: Literal['none'] = 'none'`（阶段二到阶段四 reducer 不读取搜索结果，也不做结果感知二次裁决）。
   实际的 session 写入和 handler 调用由 [message_router.py](../backend/app/services/message_router.py)（或 [conversation_service.py](../backend/app/services/conversation_service.py) 中新增的 applier 函数）按 `state_transition` 执行。
3. **frame_hint vs active_flow 冲突消解**：严格按现状文档 §3.1 表格实现。`upload_collecting → 任意 search` 在 reducer 输出 `state_transition=enter_upload_conflict + pending_interruption=...`；message_router 收到该 transition 后**调用现成的 `_enter_upload_conflict`**，不复制冲突逻辑。reducer 自身不直接调 `_enter_upload_conflict`。
4. **歧义反问（clarification）**：当 reducer 判定 `needs_clarification=true`，按 `clarification.kind` 渲染结构化追问文案（不使用 LLM 的澄清文案）。「北京有吗 + 已存在西安」默认走 `clarify`（产品策略可切到 `replace`，通过配置项 `ambiguous_city_query_policy ∈ {clarify, replace}` 控制）。
5. **置信度兜底**：当 `confidence < 0.6` 且本轮涉及关键字段（`city / job_category / salary_floor_monthly / salary_ceiling_monthly`），或 `frame_hint` 与 `active_flow` 冲突且无法消解时，reducer 强制 `needs_clarification=true`，覆盖 LLM 的判断。
6. **兼容派生层**：`DialogueDecision -> IntentResult`，按现状文档 §6.2 三维表派生，`route_intent` 字段做 `search_job / search_worker / follow_up / upload_job / upload_resume / show_more / command / chitchat`。
7. **三种灰度模式**：
   - **shadow**：默认 5% 采样（配置项 `dialogue_v2_shadow_sample_rate`），生产仍走 legacy；旁路调用新 prompt 并写日志，记录 parse / decision / legacy 三方差异，避开高峰时段（按 cron 或 QPS 阈值降级）。
   - **dual-read gated**：白名单 `dialogue_v2_userid_whitelist` 或 hash 桶 `dialogue_v2_hash_buckets`，命中的用户走 `DialogueDecision -> IntentResult` 派生路由；不命中的用户继续 legacy。
   - **primary（受控）**：阶段二**不**进入 primary；primary 留到阶段四。
8. **HIGH 风险关键词接管**：[keyword-rules-audit.md](keyword-rules-audit.md) 中 cancel / proceed / resume 三项，由 `DialogueParseResult` 中**专用的冲突解决语义**接管，**不**复用 `start_upload` / `show_more`（它们是开始上传 / 翻页，不是「继续发布」/「恢复草稿」）。`DialogueParseResult` 的 `dialogue_act` 枚举除现状文档 §3.3 列出的基础值外，新增冲突解决专用值：
   - `dialogue_act = resolve_conflict`（仅在 `active_flow=upload_conflict` 上下文下出现）；
   - 配套字段 `conflict_action ∈ {cancel_draft, resume_pending_upload, proceed_with_new}`，分别对应「取消草稿」「继续发布原草稿（恢复 pending_upload）」「先做新意图（如先找工人）」。**这里特意用 `resume_pending_upload` 而不是 `resume_upload`，避免与 frame `resume_upload`（上传简历）撞名**。

   reducer 把 `(resolve_conflict, conflict_action)` 翻译为 `state_transition`：
   - `cancel_draft → state_transition=clear_pending_upload`；
   - `resume_pending_upload → state_transition=resume_upload_collecting`；
   - `proceed_with_new → state_transition=apply_pending_interruption`（消费阶段 §2.1.3 的 `pending_interruption`）。

   关键词作为 fallback：当 LLM 未给 `resolve_conflict`、但 `active_flow=upload_conflict` 且文本命中闭集选项（如「继续发布 / 取消草稿 / 先找工人」），由 message_router 直接走对应 transition，不强求 LLM 命中。

### 2.2 边界

- **不**做 slot schema 统一（留到阶段三）。
- **不**清理 `_WORKER_SEARCH_SIGNALS / _JOB_POSTING_SIGNALS` —— 这两组在阶段一已实际接入（worker 搜索护栏），阶段二继续作为 LLM 失败时的兜底。
- **不**接入也**不**清理 `_CITY_ADD_SIGNALS / _CITY_REPLACE_SIGNALS` —— 与 §1.2 一致，这两组在仓库内为死常量，阶段一/二都**不**接入；阶段二仅保留声明不动，留待阶段四统一删除（见 §4.1.3）。
- **不**让 LLM 直接输出 `merge_policy`：prompt 只允许输出 `merge_hint ∈ {replace, add, remove, unknown}`，且明确「裸城市 / 模糊表达统一 unknown」。
- **不**新增对历史 Redis session 的迁移；新字段全部 default。
- **不**接管 [keyword-rules-audit.md](keyword-rules-audit.md) 的 MEDIUM / LOW 项。
- **不**改 reranker 链路、**不**改命令 handler 内部实现。
- **不**做结果感知对话策略：`post_search_action` 在阶段二到阶段四始终为 `none`；搜索结果出来后如何追问 / 放宽 / 软偏好排序留到 Phase 5。
- shadow 阶段**不**根据新 DTO 写 session、**不**回填 `last_intent`，只写日志。

### 2.3 改动范围

| 文件 | 改动点 |
|---|---|
| [backend/app/llm/base.py](../backend/app/llm/base.py) | 新增 `DialogueParseResult` BaseModel；`IntentExtractor` 抽象类新增 `extract_dialogue(...)`（默认 raise NotImplementedError，旧 provider 不强制实现）。 |
| `backend/app/llm/prompts.py` | 新增 `DIALOGUE_PARSE_PROMPT_V2`，独立 PROMPT_VERSION，包含 dialogue_act / frame_hint / merge_hint 的 few-shot 和「明确表达才输出 replace/add/remove」的硬约束。 |
| `backend/app/llm/`（provider 具体实现） | 实现 `extract_dialogue`，复用现有 OpenAI 兼容请求路径，记录 input/output tokens。 |
| `backend/app/services/dialogue_reducer.py`（新文件） | `DialogueDecision` BaseModel；`reduce(parse_result, session, role) -> DialogueDecision`；冲突表 / 置信度兜底 / awaiting 消费 / merge policy 决策全部在此。`post_search_action` 字段固定输出 `none`，仅作 Phase 5 兼容预留。 |
| `backend/app/services/dialogue_compat.py`（新文件） | `decision_to_intent_result(decision, session) -> IntentResult` 兼容派生。 |
| [backend/app/services/intent_service.py](../backend/app/services/intent_service.py) | **拆分 legacy 内核**：把现有 `classify_intent` 的实现体抽成 `_classify_intent_legacy(...) -> IntentResult`（不含任何 v2 分支）。新顶层入口 `classify_dialogue(text, role, history, session) -> DialogueRouteResult` 返回 `(intent_result, decision_or_none, source ∈ {legacy, v2_shadow, v2_dual_read, v2_fallback_legacy})`。`classify_intent` 保留为向后兼容包装，内部直接调 `_classify_intent_legacy`，**不再带 v2 分支**，避免 v2 失败回退时产生递归。 |
| [backend/app/services/message_router.py](../backend/app/services/message_router.py) | `_handle_text` 改调 `classify_dialogue`，拿到 `DialogueRouteResult`：v2 命中且有 `decision.clarification` 时直接渲染为 `ReplyMessage`，**不**经过 compat 派生（避免 clarification 被旧 IntentResult 吃掉）；v2 命中且无 clarification 时按 `decision.state_transition` 执行 session 写入 / handler 调用，再用 `intent_result` 走原有路由分发；shadow / 未命中走原有 `IntentResult` 路径。 |
| `backend/app/config.py` | 新增配置：`dialogue_v2_mode ∈ {off, shadow, dual_read}`、`dialogue_v2_shadow_sample_rate`、`dialogue_v2_userid_whitelist`、`dialogue_v2_hash_buckets`、`ambiguous_city_query_policy`、`low_confidence_threshold`。（仓库实际配置文件位置在 `backend/app/config.py`，不是 `backend/app/core/config.py`） |
| 日志 / 埋点 | 新增 `dialogue_v2_parse` / `dialogue_v2_decision` / `dialogue_v2_legacy_diff` 三类事件；记录 `parse / decision / legacy_intent / final_route` 四方对比，便于评估收益。 |
| `backend/tests/fixtures/dialogue_golden/` | 扩充至现状文档 §7 列出的 5 类反例（角色权限、`active_flow` 冲突、JSON 解析失败、低 confidence、awaiting 过期）。 |

### 2.4 失败模式落地

严格按现状文档 §5 表格实现。每条降级路径都要有 unit test：

- LLM JSON 解析失败：shadow 阶段忽略；dual-read 阶段在 `classify_dialogue` 内部直接调 `_classify_intent_legacy(...)` 内核（**不**再调带 v2 分支的旧入口，避免递归），记录 `dialogue_v2_fallback_to_legacy=true`。
- `dialogue_act=answer_missing_slot` 但无 awaiting：若 `slots_delta` 有效 → 重新走 `modify_search` / `start_search` 裁决；否则走 clarify / chitchat。
- `slots_delta` 字段不属于 frame schema：drop 并日志（`dialogue_v2_dropped_slots`）；drop 后无有效字段 + 本轮需业务动作 → clarify。
- `needs_clarification=true` 但无文案：用模板生成（`clarification.kind` 列表：`city_replace_or_add` / `missing_required_slot` / `frame_conflict` / `low_confidence`）。
- `cancel` 但无可取消流程：不改 session，回 `当前没有进行中的草稿/搜索需要取消`。

### 2.5 验收条件

1. **DTO / reducer 单测**：reduce 函数对每个 `(active_flow, frame_hint, dialogue_act)` 三维组合至少一条单测；冲突消解、置信度兜底、awaiting 消费、merge policy 各 ≥ 3 条覆盖。
2. **golden case 全集**：阶段一两条 happy path + 阶段二新增 5 条反例（共 ≥ 7 条）在 dual-read 模式下全绿；其中「北京有吗」case 必须断言 `clarification.kind=city_replace_or_add` 而不是文案包含。
3. **shadow 数据 ≥ 1 周**：shadow 采样 ≥ 5%，每天汇总 `parse vs legacy` 差异表（intent 误判率、frame 冲突率、clarification 占比、字段抽取一致率）。差异异常的 case 形成 case 库回灌为 golden。
4. **dual-read 灰度**：白名单 ≥ 5 个内部测试账号 + hash 桶 1%（约束以 QPS 阈值兜底），运行 ≥ 3 天，关键指标无回退（`empty_search_result_rate`、`upload_conflict_rate`、`avg_turns_to_search`）。
5. **HIGH 风险关键词接管验收**：cancel / proceed / resume 三项在 dual-read 路径下走 `dialogue_act`，关键词列表降级为 fallback。优先级顺序固定为：**显式命令（如 `/取消`、`/帮助` 等斜杠命令）/ 后端状态约束（`active_flow=upload_conflict` 中的明确确认词）> reducer 裁决 > LLM `dialogue_act` > keyword fallback**。单测必须覆盖：
   - `/取消` 即使 LLM 给了 `start_search`，仍按 cancel 处理；
   - `upload_conflict` 中用户回 `继续发布`，即使 LLM 给了 `chitchat`，仍按确认走；
   - 仅 LLM 与关键词冲突（无显式命令、无强后端约束）时，LLM `dialogue_act` 优先于 keyword fallback。
6. **回滚演练**：把 `dialogue_v2_mode` 从 `dual_read` 切回 `off`，灰度用户的下一轮请求立刻回 legacy，无 session 残留导致的状态错乱。
7. **成本评估**：shadow 阶段每天的额外 LLM token 消耗与请求数有报表，决定是否进阶段三 / 阶段四时灰度比例。

---

## 阶段三：统一 Slot Schema

定位：把字段定义从散落在 `intent_service` / `_normalize_*` / 各 handler 的硬编码，收敛为单一 schema 来源。是阶段四能放心删旧链路的前置条件。

阶段三只做字段 schema 收口，不做完整招聘 ontology 建模。`slot_schema` 可以引用未来 ontology 的概念（例如 `job_title`），但本阶段不建设 `industry / 商圈 / 职类层级网络`，避免把字段治理扩大成领域建模项目。

### 3.1 功能

1. **schema 文件**：`backend/app/dialogue/slot_schema.py`（新建），按 frame 分组（`job_search / candidate_search / job_upload / resume_upload`）。每个 slot 描述：
   - `name`：字段名（与 search_criteria / DB 字段对齐）；
   - `type`：`str / int / list[str] / enum`；
   - `normalizer`：归一化函数引用（如 `_normalize_city_value`）；
   - `required_for_action`：触发业务动作所需必填集合（搜索 vs 上传不同）；
   - `askable`：是否可追问；
   - `default_merge`：默认合并策略（`replace / add / clarify`）；
   - `filter_mode`：`hard / soft / display`。`hard` 表示当前 search_service 会做硬过滤；`soft` 表示用户偏好，可写入 criteria 但当前不硬过滤；`display` 表示仅用于展示 / 说服 / 日志归因；
   - `ranking_weight`：`float | None`。阶段三全部保持 `None`，只为 Phase 5 软偏好 reranker 预留字段位；
   - `roles`：允许使用该字段的角色集合；
   - `prompt_template`：追问文案模板（参数化，不是固定字符串）。
2. **派生能力**：
   - `compute_missing_slots(frame, criteria) -> list[str]`：阶段一的「后端 schema 重算 missing」改为调 schema。
   - `validate_slots_delta(frame, slots_delta) -> (accepted, dropped)`：reducer drop 非法字段时调 schema。
   - `default_merge_policy(frame, slot, has_old_value) -> Literal['replace','add','clarify']`：reducer 没有 `merge_hint` 时调 schema。
   - `render_clarification(kind, frame, slot, options) -> str`：clarification 文案统一生成。
   - `check_role_permission(role, frame) -> bool`：`worker` 不能 `job_upload`、`factory` 不能 `job_search` 等。
3. **schema-driven prompt**：`DIALOGUE_PARSE_PROMPT_V2` 的字段清单从 schema 自动渲染，避免 prompt 与代码 drift。
4. **招聘字段占位**：新增 `job_title` slot 占位（如服务员 / 普工 / 焊工），先标为 `filter_mode=display`、`ranking_weight=None`，允许 LLM 抽取和日志观测，但不进入 SQL 过滤、不参与 missing。`industry / 商圈 / 工种层级 ontology` 不在阶段三实现。

### 3.2 边界

- **不**改 DB 表结构、**不**改 search_service SQL 字段名。
- **不**做字段命名大改（保持 `salary_floor_monthly / job_category / city / headcount / pay_type` 等当前命名）。
- **不**做 schema 热更新 / 运营后台编辑（schema 仍是代码常量）。
- **不**实现“同字段多值带优先级”等高级语义（list 仍是无序集合）。
- **不**做完整招聘 ontology 建模；`job_title` 只是 schema 占位，不引入 `industry / 商圈 / 职类图谱`。
- **不**改 reranker prompt 字段清单。
- **不**让 reranker 消费 `filter_mode=soft` 字段；`ranking_weight` 阶段三只允许为 `None`。

### 3.3 改动范围

| 文件 | 改动点 |
|---|---|
| `backend/app/dialogue/slot_schema.py`（新文件） | 定义 schema 及所有派生函数。 |
| `backend/app/services/dialogue_reducer.py` | `validate_slots_delta` / `compute_missing_slots` / `default_merge_policy` / `check_role_permission` 全部改为调 schema。 |
| [backend/app/services/intent_service.py](../backend/app/services/intent_service.py) | `_sanitize_intent_result` / `_normalize_structured_data` / `_normalize_int_field` / `_normalize_string_list` 等函数引用 schema 中的 normalizer，避免重复定义。`_SEARCH_FIELD_REMAP` 通过 schema 表达「同义字段」。 |
| `backend/app/llm/prompts.py` | `DIALOGUE_PARSE_PROMPT_V2` 字段清单从 schema 渲染（启动时一次性构建字符串常量）。 |
| [backend/app/services/message_router.py](../backend/app/services/message_router.py) | 上传追问文案、搜索追问文案统一由 `render_clarification` / `prompt_template` 生成；保留现有固定文案常量作为 fallback。 |
| 测试 | schema 自身单测（覆盖所有 frame × slot）；reducer 单测增加 schema 边界（非法 enum、超界 int、未授权角色）。 |

### 3.4 验收条件

1. schema 自身单测覆盖每个 slot 的 normalizer / 边界值（含 None、空串、非法 enum、列表去重、负数、超大整数）。
2. **不再存在独立维护的权威字段清单**：`intent_service` / `dialogue_reducer` 中用于校验 / 归一 / 派生的字段集合必须从 schema 派生（`_SEARCH_FIELD_REMAP` 改由 schema 中的「同义字段」表达，`_VALID_JOB_KEYS` / `_VALID_RESUME_KEYS` 改为 `slot_schema.fields_for(frame)`）。允许在以下位置出现字段字面量：测试用例的断言、日志 / 埋点字段名、兼容映射表、prompt few-shot 文本——这些不构成「权威清单」。
3. 所有阶段二 golden case 在 schema-driven 路径下重跑全绿。
4. 角色权限单测：worker 触发 `start_upload + job_upload` → 拒绝并 clarify；factory 触发 `start_search + job_search` → 拒绝或转 `candidate_search`（按现状文档 §5 「角色无权限」表项）。
5. `compute_missing_slots` 与阶段一旧版结果的差异 ≤ 1%，差异 case 全部由 schema 升级解释（不是 bug）。
6. `provide_meal / provide_housing / shift_pattern` 等软偏好字段在 schema 中标为 `filter_mode=soft`，可抽取可写入 criteria，但不会被 missing 追问，也不能断言 SQL 召回数量变化。
7. `job_title` 可被抽取并保留在 criteria / 日志中，但 search_service 不消费；golden case 只断言字段归一和不误触发 missing，不断言排序或召回变化。

---

## 阶段四：扩大灰度并替换旧链路（Primary mode）

定位：在 schema 稳定 + dual-read 数据健康的基础上，把新 DTO 升为主链路，旧 `IntentResult` 仅作为 fallback。

### 4.1 功能

1. **primary mode**：`dialogue_v2_mode = primary`。新 DTO 是 source of truth，路由从 `DialogueDecision` 直接派生，不再 dual-read 切换。
2. **legacy 退化为 fallback**：仅当 `extract_dialogue` 抛错或 JSON 解析失败时，回退到 `_classify_intent_legacy(...)` 内核（不调旧的 `classify_intent` 顶层入口，避免在 primary 路径里产生递归），且打 `dialogue_v2_fallback_to_legacy=true` 日志。
3. **关键词规则清理（区分两类）**：
   - **删除**：开放式中文关键词兜底（HIGH 项中用于推断用户自由表达 cancel / proceed / resume 的中文词表）。这些词表过去**主导语义**，primary 下由 `resolve_conflict` + `conflict_action` 接管，兜底词表不再需要。
   - **保留**：
     - **斜杠命令**（`/取消`、`/帮助`、`/重置` 等）：闭集，确定性解析，不属于「关键词膨胀」。
     - **系统提示中的闭集选项**（如 `upload_conflict` 提示「继续发布 / 先找工人 / 取消草稿」），用户回相应短语的解析仍保留，作为 LLM 未识别时的安全兜底；这是产品给定的有限选项集，不是开放词表。
     - **legacy fallback 链路所需的最小关键词集**：在 `_classify_intent_legacy` 中保留确认 / 取消的最小词表，确保 v2 解析失败回 legacy 时仍有基本的取消 / 继续保护。
   - MEDIUM 项中不再被引用的常量删除：`_WORKER_SEARCH_SIGNALS` / `_JOB_POSTING_SIGNALS` 在阶段一被实际接入（worker 搜索护栏），primary 下 `dialogue_act` 接管后清理引用并删除常量；`_CITY_ADD_SIGNALS` / `_CITY_REPLACE_SIGNALS` **从未接入**，属于历史遗留死代码，阶段四直接删除常量声明。同步更新 [keyword-rules-audit.md](keyword-rules-audit.md) 标记。
4. **`criteria_patch` 兼容收口**：[base.py](../backend/app/llm/base.py) 中 `criteria_patch` 字段保留 schema（旧 provider 兼容），但新 prompt 不再要求 LLM 输出；后端不再消费 `criteria_patch` 的 `op` 语义，仅在 legacy fallback 路径下保留。
5. **策略配置收口**：把分散的对话策略配置集中到 `settings.dialogue_policy` 子结构（或等价 Pydantic settings model），至少包含 `ambiguous_city_query_policy / low_confidence_threshold / search_awaiting_ttl_seconds / dialogue_v2_mode / shadow_sample_rate / rollout_percentage`。阶段四只做单机配置组织，不做多租户 / 多渠道策略系统。
6. **灰度推进节奏**：
   - 第 1 周：primary 灰度 5%（hash 桶）。
   - 第 2 周：25%，监控关键指标（搜索成功率、conflict 率、平均轮数、clarify 占比）。
   - 第 3 周：50%。
   - 第 4 周：100%。
   - 任一阶段关键指标回退 ≥ 5% 立即回滚到 dual-read。

### 4.2 边界

- **不**删 `IntentResult` 类型本身，旧 handler / fallback 仍依赖。
- **不**删 [base.py](../backend/app/llm/base.py) 的 `IntentExtractor.extract` 抽象方法（保持向后兼容）。
- **不**改 reranker 链路。
- **不**做 session schema 破坏性迁移；旧字段（`criteria_patch` 历史日志、`current_intent`、`last_intent`）保留。
- **不**在阶段四做模型升级 / 切换 provider（保持当前 LLM 配置）。

### 4.3 改动范围

| 文件 | 改动点 |
|---|---|
| `backend/app/config.py` | `dialogue_v2_mode` 枚举增加 `primary`；增加 `primary_rollout_percentage`（hash 桶百分比）；把对话策略类配置集中到 `dialogue_policy` 子结构。（仓库实际配置位置；`backend/app/core/config.py` 不存在） |
| [backend/app/services/intent_service.py](../backend/app/services/intent_service.py) | primary 模式下 `classify_dialogue` 优先调 v2 路径；v2 失败时调 `_classify_intent_legacy(...)` 内核（不再回到带 v2 分支的入口，避免递归）。删除 `_WORKER_SEARCH_SIGNALS / _JOB_POSTING_SIGNALS / _CITY_ADD_SIGNALS / _CITY_REPLACE_SIGNALS` 四组常量及引用。 |
| [backend/app/services/message_router.py](../backend/app/services/message_router.py) | 删除 dual-read / shadow 分支中已废弃的临时代码；所有路由从 `DialogueDecision` 派生。 |
| `backend/app/llm/prompts.py` | 删除旧 `IntentResult` prompt 中已不再使用的 few-shot；保留 fallback prompt。 |
| `docs/keyword-rules-audit.md` | HIGH 项标记为「已接管」；MEDIUM 项更新接管/保留状态；新增「阶段四清理记录」段落。 |
| 监控大盘 | 新增 primary 模式专用监控面板：v2 成功率、fallback 率、clarify 率、按 frame 拆分的搜索成功率。 |

### 4.4 验收条件

1. primary 100% 灰度后稳定运行 ≥ 2 周，关键指标对照阶段二基线无回退：
   - 空结果率（`empty_search_result_rate`）≤ 基线 +1%；
   - upload_conflict 率 ≤ 基线 +1%；
   - 平均到达搜索的轮数（`avg_turns_to_search`）≤ 基线 +0.2；
   - LLM JSON 解析失败率 ≤ 0.5%。
2. fallback 率 ≤ 1%，且 fallback case 周抽样 20 条人工评估「legacy 是否给出了正确路由」，正确率 ≥ 95%。
3. [keyword-rules-audit.md](keyword-rules-audit.md) HIGH 项中**开放式中文兜底词表**全部删除，配套主路径测试改为断言 `dialogue_act` / `conflict_action` 而非关键词命中；斜杠命令、`upload_conflict` 闭集选项、`_classify_intent_legacy` 内最小确认 / 取消词表保留并明确标记为「fallback-only」，对应单测改为断言 fallback 路径行为。
4. 旧 `criteria_patch` 在 primary 路径下未被消费（grep 调用点为 0 或仅在 legacy fallback 模块内）。
5. 回滚演练：从 primary 切回 dual-read 一次，5 分钟内灰度比例归零，session 无残留状态错乱（用 worker / broker 两种角色各演练 1 次）。
6. 旧 Redis session（阶段一前创建的）反序列化无报错，按默认值进入 primary 流程能正常完成一轮搜索。

---

## 跨阶段共同约束

以下原则贯穿全部四个阶段，任何阶段违反都视为不达标：

1. **LLM 不写 session**：任何 LLM 输出都必须经 schema 校验和 reducer 裁决后才能落 `SessionState`。（来自现状文档 §5、§9）
2. **active_flow 是 source of truth**：`frame_hint` 仅作为本轮信号，不长期持久化。
3. **新增 SessionState 字段必须 default**：旧 Redis session 反序列化不能失败；测试 fixture 同步补默认值。
4. **clarification 文案不依赖 LLM**：用结构化 `clarification.kind` 在 schema / 模板中渲染，避免易碎文案断言。
5. **golden case 长期保留**：每个阶段引入的 case 在后续阶段不得删除，只能升级断言。
6. **回滚优先于修补**：任何阶段的灰度切流，遇到关键指标回退 ≥ 5% 都立即回滚而非热修；热修必须有对应的 golden case 增量。
7. **关键词列表只减不增**：阶段二之后禁止新增中文关键词列表来解决开放语言问题（来自现状文档 §9）。

---

## Phase 5+ 路线展望（不属于当前四阶段）

以下方向用于保持方案的长期通用性和招聘业务适配性，但**不进入阶段一到阶段四的验收范围**。当前四阶段的目标仍是把对话理解、状态裁决、schema 收口和 primary 替换做稳。

### Phase 5：Result-aware Dialogue Policy

Phase 5 独立处理「搜索结果出来后系统如何继续推进对话」，不塞进阶段三 schema。

1. **二阶段裁决**：新增 `post_search_reducer(search_result, session, decision) -> PostSearchDecision`，输入搜索结果和阶段二 `DialogueDecision`，输出 `post_search_action`，例如 `show_results / ask_clarification / auto_relax_and_retry / suggest_relaxation / paginate`。
2. **0 结果策略**：在 SQL fallback 已有放宽能力的基础上，决定何时自动放宽、何时反问用户、何时只给放宽建议，避免把所有 0 结果都包装成普通空结果。
3. **`show_more` 降级语义**：当用户说「还有吗」但当前候选为空或已翻完时，区分继续翻页、建议放宽、让用户换城市 / 工种 / 薪资。
4. **软偏好排序**：让 reranker 消费阶段三 schema 中 `filter_mode=soft` 且 `ranking_weight` 非空的字段（如包吃 / 包住 / 班次），但必须单独评估排序影响，不在阶段三启用。
5. **可见性文案**：当用户表达软偏好但当前只能软排序时，由结果回复模板说明「会优先展示符合偏好的岗位」，不把 `user_visibility` 做成 slot schema 字段。

Phase 5 启动前，阶段二到阶段四里的 `post_search_action` 必须保持 `none`，避免结果感知逻辑半接入、半失效。

### Phase 6+：招聘领域建模

完整 ontology 建设独立于对话路由四阶段，包括 `job_title / job_category / job_sub_category / industry / 商圈 / 工业园 / 技能证书` 等概念关系。阶段三只保留 `job_title` 占位，不建设领域图谱。

- `slot_schema` 是字段契约；ontology 是领域概念网络。schema 可以引用 ontology 的 canonical id，但不能把 ontology 规则硬塞进 reducer。
- per-field provenance（`raw_value / normalized_value / confidence / source_text`）暂不做。需要分析时，先基于 `raw_response` 和日志回放；若后续要做字段级标注平台，再单独设计 LLM JSON schema、日志落库和回放链路。

### 长期边界说明

- `multi-slot` 已支持：一句话里同时抽 `city + salary + provide_housing` 是正常 `slots_delta`。
- `multi-value` 已支持：一句话里「服务员也可以收银」可以落成同一字段的多个候选值。
- `sequential intent` 暂不支持：例如「先看苏州，不行再看北京」需要后续计划队列，不属于当前 reducer 范围。
- `reference resolution` 暂不支持：例如「这个岗位怎么联系」需要锚定 `last_shown_item`，更适合建独立的岗位详情 / 联系 frame，而不是归入多意图嵌套。
- 多租户 / 多渠道策略系统暂不做。当前只做 `dialogue_policy` 配置收口；等真的出现第二渠道或客户级差异，再设计配置存储、审计和灰度。

---

## 阶段依赖关系一览

```text
阶段一（收紧 + awaiting 物化）
   └── 阶段二（DTO + reducer + shadow/dual-read）
          └── 阶段三（统一 slot schema）
                 └── 阶段四（primary + 清理 legacy 关键词）
```

- 阶段一 → 阶段二：阶段一未通过验收前不引入新 DTO，避免在不稳定基线上做架构改造。
- 阶段二 → 阶段三：阶段二 dual-read shadow 数据 ≥ 1 周后启动阶段三；schema 工作可与阶段二后期并行准备，但不上线。
- 阶段三 → 阶段四：schema 替换完成后才能进 primary，否则 primary 模式下硬编码字段清单仍会与新 prompt drift。
- 阶段四不引入新功能，只做替换和清理。
