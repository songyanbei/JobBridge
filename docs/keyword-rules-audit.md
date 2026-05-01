# 硬编码关键词规则审计

调研日期：2026-04-28
扫描范围：`backend/app/` 全量服务代码

## 0. 事故回放 → 审计动机

### 0.1 现象

broker `wm_mock_broker_001` 在 mock-testbed 里跑这段对话：

```
/找工人          → 已切换到【找工人】模式
机械厂普工       → 信息还不够完整，请补充：期望城市
北京             → 暂未找到匹配的求职者。可以放宽城市或工种条件…
换成苏州         → 暂未找到匹配的求职者。可以放宽城市或工种条件…
```

但运营后台简历管理界面里 ID=12（dev_worker_001）那条简历**完全应该命中**——
`expected_cities=['苏州市','无锡市']`、`expected_job_categories=['电子厂','普工']`、
`audit_status=passed`、未过期、owner `status=active`。

### 0.2 取证

从 `jobbridge-worker-1` 容器日志拿到三轮的 `criteria_snapshot`：

| 轮次 | 用户输入 | session.search_criteria（解码后） |
|---|---|---|
| 1 | 机械厂普工 | `{"expected_job_categories": ["普工"]}` |
| 2 | 北京 | `{"expected_job_categories": ["普工"], "expected_cities": ["北京"]}` |
| 3 | 换成苏州 | `{"expected_job_categories": ["普工"], "expected_cities": ["北京", "苏州"]}` |

定位出三个独立 bug 同时叠加：

**Bug A（致命）：字段名错位。** LLM 把搜索条件落到了 `expected_cities` / `expected_job_categories`，
而 [search_service._query_resumes](../backend/app/services/search_service.py:493) **只读 `criteria["city"]` 和 `criteria["job_category"]`**。
criteria 里没有这两个键 → [has_effective_search_criteria](../backend/app/services/search_service.py:431)
判为"无有效条件" → SQL 直接返回 `[]` → 温和 fallback 全 0、激进 `keep_city_only`
探查也 0（它复制的还是 `expected_*`）→ 落到 `NO_WORKER_MATCH_REPLY` 兜底文案
（而不是带 suggestions 的版本）。

为什么 LLM 这么抽？[prompts.py 第 79 行](../backend/app/llm/prompts.py:79)（v2.1）原文：
> "检索条件中 city、job_category、expected_cities、expected_job_categories
> 统一按 list[str] 存储，即便只有 1 个值也存列表。"

这句话直接误导 LLM：broker 在找工人，工人简历用 `expected_*`，那搜索条件也用 `expected_*`
吧？few-shot 里没有 broker `search_worker` 反例，LLM 自由发挥。

**Bug B（结构性）：city 短名 vs 全名。** 第 2 / 3 轮 LLM 输出的是 `"北京"` / `"苏州"` 不带"市"，
但 DB 里 `expected_cities` 是 `["苏州市","无锡市"]`，`JSON_CONTAINS` 字面量比对必为 false。
[`dict_city`](../backend/sql/seed.sql:36) 表里有 `short_name='苏州'` / `aliases=['姑苏',...]`，但服务端
[`_coerce_field_value`](../backend/app/services/intent_service.py:444) 的 city 分支
**没有任何 short_name → name 归一**，纯靠 prompt 约束 LLM 输出"苏州市"，一旦 LLM 漂移就翻车。

**Bug C（增量错位）：follow_up 用 `add` 而非 `update`。** 第 3 轮"换成苏州"理应替换 city，
但 LLM 给的 patch 是 `add` → 合并后 city = `["北京","苏州"]`。这条不是本次 0 命中的根因
（Bug A 在前面已经截断），但是个独立隐患。

### 0.3 修复（已落地）

**Bug A：[prompts.py](../backend/app/llm/prompts.py) v2.1 → v2.2**
- 改写第 79 行那句"统一存储"，明确分流：搜索/follow_up → `city`/`job_category`，简历上传 → `expected_*`，岗位上传 → `city`/`job_category` 标量。
- 加 3 条 broker 找工人 few-shot（首轮 search_worker、follow_up 补 city、"换成 X" 应输出 `update`）。
- 服务端 [intent_service.py](../backend/app/services/intent_service.py) 加 `_SEARCH_FIELD_REMAP` 兜底：搜索 intent 上把 LLM 漂移给的 `expected_cities`/`expected_job_categories` 重映射到 `city`/`job_category`，`structured_data` / `criteria_patch` / `missing_fields` 三个出口都覆盖。

**Bug B：[intent_service.py](../backend/app/services/intent_service.py)**
- 加 `_get_city_lookup` 进程级缓存，lazy 从 `dict_city` 加载 `name + short_name + aliases → 规范名`。
- `_coerce_field_value` 的 city 分支每个值过一遍 `_normalize_city_value`。
- 配 `_clear_city_lookup_cache()` 给运营改完别名后清缓存用。

**Bug C：follow_up 改"输出全量 criteria 快照"语义** —— 选了 §6.2 路线，结构性消解 add/update 二元歧义：
- prompt v2.2 → v2.3：follow_up 的 `structured_data` 必须输出"应用本句变更后的完整 criteria 快照"（包括本句没动的字段，从 `current_criteria` 原样保留）；`criteria_patch` 留空。三条 follow_up few-shot（示例 4 / 9 / 10）改写成全量快照；新增示例 11 演示叠加语义。
- 后端 [conversation_service.replace_criteria](../backend/app/services/conversation_service.py:185)：新函数，全量替换 `session.search_criteria` 并处理 digest / 快照失效。
- [_handle_follow_up](../backend/app/services/message_router.py:729)：`structured_data` 非空 → 走 `replace_criteria`；为空 → 回落到 `merge_criteria_patch`（兼容降级）。
- "换成 X" 在 LLM 这一层就直接产出新值替换原列表，根本没有 op 概念，物理上不可能再标错。

测试：相关 unit 套（intent / search / conversation / message_router / Stage B/C1 / llm_prompts）232 passed / 0 failed。完整 unit 套 690 passed / 5 failed（5 个失败为预存在，与本次无关）。

### 0.4 为什么有了 0.3 还要做这次审计

讨论 Bug C 时考虑过给 `merge_criteria_patch` 配关键词列表
（"换成"/"改成"/"只看"/"现在改"/"不要 X 了" → 强制 update）。
最终否决：**关键词列表永远穷举不全**。Bug C 改成 §6.2 路线（follow_up 输出
全量 criteria 快照），歧义点物理消失，比加关键词清单干净得多。

但同一个反模式在 backend 里不止 Bug C 一处。本次审计的目的就是
把所有"用硬编码中文关键词列表做业务分支判断"的地方扒一遍，
评估各自的可枚举性和漏词风险，给出按优先级排序的改进路径。

---

## 结论先行

后端共 **16 处**用关键词 / 同义词 / 字面量短语做业务分支判断的地方。其中：

| 数量 | 类别 | 风险 | 说明 |
|---|---|---|---|
| 9 | **封闭枚举**（命令、操作 op、角色、字段元数据） | LOW | 取值空间天然有限，无漏词概念 |
| 4 | **半开放（已有兜底）** | MEDIUM | 工种 / 城市同义词 / 阶段一临时对话信号，依赖运维同步或后续 dialogue_act 接管 |
| 3 | **开放集且关键路径上**（cancel / proceed / resume / patch 启发式） | **HIGH** | 用户自然语言的二元/三元选择，漏词导致流程错位、草稿污染、死循环防护误触发 |

3 个 HIGH 项都集中在 [message_router.py](../backend/app/services/message_router.py) 的多轮上传流程，是这次最值得重构的部分。

---

## 一、封闭枚举（合理，无需改动）

下列关键词集都是**取值空间天然有限**的，漏不漏词不存在 —— 不在集合内的取值就是非法 / 未知，按设计走默认分支即可。

| # | 名称 | 位置 | 用途 |
|---|---|---|---|
| 1 | `_COMMAND_MAP` + `_PARAM_COMMAND_PREFIXES` | [intent_service.py:34](../backend/app/services/intent_service.py:34), [85](../backend/app/services/intent_service.py:85) | `/帮助` `/续期` 等显式命令别名归并；漏写的别名会回落到 LLM 自然语言识别 |
| 2 | `_SHOW_MORE_PATTERNS` | [intent_service.py:96](../backend/app/services/intent_service.py:96) | "更多 / 换一批 / 下一页" 等翻页同义语。漏词只导致回落到 LLM 重分类，无业务破坏 |
| 3 | `_JOB_CATEGORY_CANONICAL` | [intent_service.py:139](../backend/app/services/intent_service.py:139) | 10 类工种闭集。LLM 输出不在闭集里时，规整层走"未知保留 + warning"路径 |
| 4 | `_VALID_PATCH_OPS` | [intent_service.py:127](../backend/app/services/intent_service.py:127) | `{add, update, remove}`，patch 操作合法值 |
| 5 | `_SEARCH_INTENTS` / `_SEARCH_FIELD_REMAP` | [intent_service.py:185](../backend/app/services/intent_service.py:185) | 搜索 intent 集合 + `expected_*` → `city/job_category` 重映射 |
| 6 | `_LIST_FIELDS` / `_INT_FIELDS` | [intent_service.py:166](../backend/app/services/intent_service.py:166), [170](../backend/app/services/intent_service.py:170) | 字段类型元数据 |
| 7 | 角色枚举 `worker/factory/broker` | [models.py:24](../backend/app/models.py:24) 等多处 | 数据库 enum 强约束 |
| 8 | `_ALLOWED_ACTIONS` / `_ALLOWED_TARGET_TYPES` | [admin_log_service.py:19](../backend/app/services/admin_log_service.py:19) | 审计日志合法 action 枚举 |
| 9 | `_ALLOWED_RENEW_DAYS` | [command_service.py:277](../backend/app/services/command_service.py:277) | `/续期 N` 仅接受 {15, 30} |

---

## 二、半开放集（已有多层兜底，但仍依赖维护同步）

### 2.1 工种同义词字典 `_JOB_CATEGORY_SYNONYMS`

- **位置**：[intent_service.py:142](../backend/app/services/intent_service.py:142)
- **规则**：`{"厨师": "餐饮", "操作工": "普工", ...}`，24 条口语化工种 → canonical 大类。
- **匹配层级**：精确 → 小写 → 子串包含（`if syn in text`）。
- **漏词后果**：LLM 输出的工种值找不到映射 → keep-as-is → SQL 用 `JSON_CONTAINS(...)` 字面量比对 → **0 命中**。
- **兜底来源**：
  1. **LLM prompt 已闭集约束**（[prompts.py:55-65](../backend/app/llm/prompts.py:55)）：明确列出 10 个 canonical + 24 条同义词归并规则，要求 LLM 不得输出原词。
  2. **服务端规整层子串包含**（[intent_service.py:473](../backend/app/services/intent_service.py:473)）：`for syn in synonyms: if syn in text` 救场。
- **风险等级**：MEDIUM —— 真正漏掉一个新工种词（同时绕过 prompt 闭集约束 + 子串包含 + warning）的概率不高，但发生时直接 0 命中。
- **推荐改进**：把 `_JOB_CATEGORY_SYNONYMS` 与 prompt 同义词列表的同步关系写进 `prompts.py` 注释；新增同义词时在 CI 里做 diff 比对（两处必须同步）。

### 2.2 城市别名（DB 源）`_CITY_LOOKUP`

- **位置**：[intent_service.py:194-224](../backend/app/services/intent_service.py:194)
- **规则**：从 `dict_city` 表加载 `name + short_name + aliases` → 规范名 (name)。**不是硬编码**，但效果上是个关键词字典。
- **漏词后果**：DB 里某城市的 aliases 没维护齐 → 用户用未录入的简称 → 字面量比对失败 → 搜索 0 命中。
- **优势**：**可由运营在 admin 后台 `/dict/cities` 维护**，不需要改代码 + 重启。
- **风险等级**：MEDIUM —— 漏的"词"只是别名 JSON 数组，可运维补救。
- **推荐改进**：把"补别名"作为运维 SOP 写入文档；admin 改完后自动 invalidate 缓存（当前需要重启或调用 `_clear_city_lookup_cache()`）。

### 2.3 上传草稿过期前的"看起来像 patch"启发式

- **位置**：[message_router.py:97-111](../backend/app/services/message_router.py:97), [`_looks_like_upload_patch`:841](../backend/app/services/message_router.py:841)
- **规则**：
  - `_PATCH_RE_HEADCOUNT` / `_PATCH_RE_DIGIT` / `_PATCH_RE_SALARY` 三条正则
  - `_KNOWN_SHORT_PATCH_KEYWORDS`：12 个常见工种短词
  - `_KNOWN_CITIES`：22 个常见招聘城市
  - 任一命中即 `True`
- **触发场景**：上传草稿过期（>10 分钟）后，用户再发消息时，决定是回"草稿已超时"还是当作新请求放行。
- **漏词后果**：LOW —— 走错分支只影响过期提醒文案，不影响数据。最坏情况是漏掉提醒，用户感受不到差异。
- **风险等级**：MEDIUM（按位置归类），**实际影响 LOW**。
- **推荐改进**：可不动；如要清理，把 `_KNOWN_CITIES` 替换成查 `dict_city`。

### 2.4 Bug 6 阶段一临时对话信号

- **位置**：[intent_service.py](../backend/app/services/intent_service.py) v2.4 常量区
- **规则**：
  - `_WORKER_SEARCH_SIGNALS`：worker 找岗位信号，如“找 / 想找 / 求职 / 工作 / 有吗”
  - `_JOB_POSTING_SIGNALS`：岗位发布信号，如“招聘 / 招工 / 急招 / 要人 / 缺人”
  - `_CITY_ADD_SIGNALS`：城市追加信号，如“也行 / 也可以 / 加上 / 还看”
  - `_CITY_REPLACE_SIGNALS`：城市替换信号，如“换成 / 改成 / 只看 / 有吗”
- **触发场景**：阶段一兜底 worker 搜索 intent 误判、城市短追问替换/追加语义等事故链路。
- **漏词后果**：MEDIUM —— 漏词可能让 LLM 重新主导判断，导致 worker 找工作被误判为发布岗位，或“北京有吗”沿用旧条件。
- **当前定位**：这是允许存在的**临时护栏**，不是长期方案。它和 [dialogue-intent-extraction-current-state.md](dialogue-intent-extraction-current-state.md) 中的 `dialogue_act` / `merge_hint` / reducer 目标有明确接替关系。
- **推荐改进**：进入阶段二后由 `DialogueParseResult` + reducer 接管，保留这些 tuple 只作为低优先级 fallback；不得继续无限扩词表。

---

## 三、HIGH 风险：开放集 + 关键路径

下列三处都在多轮上传冲突解决（`upload_collecting` / `upload_conflict`）的关键分支上，漏词直接导致**用户被困流程**。

### 3.1 取消草稿强规则 `_CANCEL_FULL` / `_CANCEL_PREFIX`

- **位置**：[message_router.py:93-94](../backend/app/services/message_router.py:93), [`_is_cancel`:831](../backend/app/services/message_router.py:831)
- **规则**：
  ```python
  _CANCEL_FULL = {"取消", "不发了", "算了", "先不发了", "不要了"}
  _CANCEL_PREFIX = ("不发", "先不", "算了，", "算了,")
  ```
- **匹配方式**：完整句全等 OR 句首前缀 → 判 cancel → **清空 pending_upload，丢弃草稿**。
- **触发场景**：用户在补字段过程中表达放弃。
- **可枚举性**：❌ 开放集。"停一下吧 / 算了吧 / 别发了 / 不用了 / 等等先 / 暂停 / 放弃" 都是 cancel 同义但都不在表里。
- **漏词后果**：
  - 用户的取消意图被当作"字段补丁"
  - 几次解析失败后 `failed_patch_rounds++`，触发"信息仍不完整，请整段重新发送"的冰冷提示
  - 用户体感：明明说了"算了"系统怎么还在追问？
- **当前防护**：仅前缀匹配 + LLM intent 兜底（但 LLM 在短句上常分类成 chitchat 而非 cancel）。
- **风险等级**：🔴 **HIGH**

### 3.2 冲突解决 proceed 信号 `proceed_keywords`

- **位置**：[message_router.py:449-453](../backend/app/services/message_router.py:449)
- **规则**：
  ```python
  proceed_keywords = ("先找", "找工人", "找岗位", "看简历", "看岗位", "看看")
  has_proceed_signal = (
      any(p in content for p in proceed_keywords)
      or intent in ("search_job", "search_worker")
  )
  ```
- **触发场景**：upload_conflict 状态下，用户应在"继续发布 / 先找工人或找岗位 / 取消草稿"三选一，本规则识别"先找工人/岗位"那一支。
- **可枚举性**：❌ 开放集。"先搜搜看 / 帮我找找 / 看下有什么人 / 先看候选人" 都是 proceed 同义但不在表里。
- **当前防护**：✓ `or intent in ("search_job", "search_worker")` 提供 LLM intent 兜底 —— 如果 LLM 识别出搜索意图就走 proceed，**这条兜底显著降低了 keyword 漏词的实际风险**。
- **真实风险**：仅当 LLM 把搜索意图误识别成 chitchat / follow_up（短句、模糊表达常见）时，keyword 才是唯一防线。
- **漏词后果**：
  - 既不命中 proceed 也不命中 resume / cancel → 进入死循环防护
  - 第二次还漏 → `conflict_followup_rounds=2` → 系统强制丢弃草稿，回 `CONFLICT_DEAD_LOOP_REPLY`
- **风险等级**：🔴 **HIGH**（但有 LLM intent 兜底，实际触发概率比 cancel 低）

### 3.3 冲突解决 resume 信号 `resume_keywords`

- **位置**：[message_router.py:464](../backend/app/services/message_router.py:464)
- **规则**：
  ```python
  resume_keywords = ("继续发布", "继续填", "继续", "接着发", "接着")
  ```
- **匹配方式**：substring 包含 + 优先级低于 proceed_keywords。
- **可枚举性**：❌ 开放集。"接着填吧 / 那继续吧 / 接着补 / 还填" 都同义。
- **漏词后果**：用户表达"继续"但不在表里 → 走死循环防护 → 草稿被强丢。
- **当前防护**：仅 substring 匹配，无 LLM 兜底（resume 不像 proceed 有 search intent 可对齐）。
- **风险等级**：🔴 **HIGH**

---

## 四、复现用例

### 用例 A：cancel 同义词漏掉（HIGH）
```
用户：苏州电子厂招普工30人
机器人：好的，请告诉我月薪范围
用户：停一下吧，我再想想       ← "停一下"不在 _CANCEL_FULL/_PREFIX
机器人：信息还不够完整，请补充：月薪
用户：算了                      ← 这次命中 _CANCEL_FULL
机器人：草稿已取消
```
**问题**：用户第一次的取消被当作字段补丁失败，体验割裂。

### 用例 B：proceed 同义词漏掉但 LLM 救场（依赖运气）
```
用户：苏州电子厂招30人
机器人：请补充月薪
用户：先帮我搜搜有没有合适的工人 ← "搜搜"不在 proceed_keywords
                                    但 LLM 可能识别 intent=search_worker → 救场
```
**问题**：依赖 LLM 把短句"搜搜"分类为 search_worker；分类为 follow_up 或 chitchat 时就走死循环。

### 用例 C：resume 同义词漏掉（HIGH，无兜底）
```
用户：苏州电子厂招普工
机器人：（追问月薪）；中途用户被打断
用户：先看下苏州的工人
机器人：要继续发布草稿、先看简历，还是取消？
用户：接着填吧                  ← "接着填" substring 不命中"继续"也不命中"接着发"
                                  （命中"接着"！实际 OK）
用户：那继续来吧                ← substring 命中"继续" ✓
```
仔细看 resume_keywords 用 substring 匹配，覆盖面比看上去宽（"接着" 是任何"接着..."的子串）。**实际可漏的是用户用"那就这样吧 / 行 / 好"等纯语气词**，会同时绕过 resume / proceed / cancel 三套规则，进入死循环。

---

## 五、为什么"扩词表"不是好答案

讨论的起点是 follow_up 里"换成 X"被识别为 `add` 而非 `update` —— 当时考虑用 `("换成", "改成", "只看", "现在改", "不要 X 了") → update` 这种关键词兜底。这个方向对**所有**开放集规则都不可取，原因：

1. **穷举不可能**：每加一条 case 都是临时打补丁，词表会无限膨胀且永远落后于真实表达多样性。
2. **歧义难处理**："改成" 也可能出现在"改成需要 5 个人"（headcount patch）里，关键词层无法区分语境。
3. **维护成本高**：词表散落在各业务模块，没有统一来源，很难做 regression。
4. **LLM 已在那里**：项目已经为意图分类付了 LLM 调用的成本，关键词兜底其实是在**重做 LLM 应该做的事**。

---

## 六、改进方向（按改动成本排序）

### 6.1 短期（仅改 prompt + 0 行代码）

把"二元/三元选择"的语义规则压给 LLM，**不依赖关键词清单**：

- **follow_up 的 op 选择**：在 prompt 里写"用户表达替换/否定原条件 → `update`；表达叠加 → `add`；裸值默认 `update`"，配 3 条对照 few-shot（已在我们刚改的 prompts.py v2.2 里加了"换成 X"示例）。
- **upload_conflict 的三选一**：让 LLM 直接在意图层就把消息分为 `intent ∈ {cancel_pending, resume_upload, proceed_search, ambiguous}`，message_router 拿到 enum 直接分发，不再用关键词。
- **cancel 强规则**：同上，让 `intent_service` 输出 `intent=cancel_pending`，message_router 不再 `_is_cancel(content)`。

成本：扩 prompt + 1-2 条 few-shot，几乎不改代码。

### 6.2 中期（架构性收拢）

- **follow_up 改为"输出全量 criteria"**：取消 `criteria_patch` 的 op 维度，LLM 基于 `current_criteria + 新消息` 直接输出新一轮完整 criteria，后端无脑替换。`add/update` 的歧义点物理消失。详见上一轮讨论的方案 B。
- **upload_conflict 的三选一固化为 LLM 分类输出**：用结构化 enum 替代关键词。

### 6.3 长期（数据层）

- 把 `_JOB_CATEGORY_SYNONYMS` 从代码搬到 `dict_job_category.aliases`（DB），与城市同构，admin 可维护、运行时缓存、prompt 自动从 DB 渲染。这样三处同义词来源（代码 / DB / prompt 文档）合一。

---

## 附：优先级建议

按"修复成本 / 影响面"排序：

1. **§3.1 cancel 强规则** → §6.1 短期方案（让 LLM 输出 `intent=cancel_pending`）。修一处，砍掉一整类漏词风险。
2. **§3.3 resume_keywords**（无 LLM 兜底）→ 同上方案。
3. **§3.2 proceed_keywords** → 已有 LLM intent 兜底，优先级低；可在 §3.1/§3.3 一起做的时候顺便。
4. ~~**follow_up `add` vs `update` 歧义** → §6.2 改全量 criteria，结构性消解。~~ ✅ **已修（2026-04-28，Bug C，prompt v2.3）**
5. **§2.1 工种同义词同步** → CI 加 prompt vs `_JOB_CATEGORY_SYNONYMS` 同步检查。
6. **§2.2 城市别名** → 文档化运维 SOP 即可。

---

## 阶段二接管说明（2026-05-01）

dialogue-intent-extraction-phased-plan §2 阶段二已落地（`DialogueParseResult` /
`DialogueDecision` / reducer / dual_read 模式）。本节记录三个 HIGH 风险项的接管状态：

### HIGH 项接管：cancel / proceed / resume

dual_read 路径下由 `DialogueParseResult.dialogue_act` 接管，关键词列表降级为 fallback：

- `dialogue_act = cancel`：`§3.1` 自然语言 cancel 表达由 LLM 解析；`/取消` 仍走斜杠命令。
- `dialogue_act = resolve_conflict + conflict_action ∈ {cancel_draft,
  resume_pending_upload, proceed_with_new}`：接管 `§3.2 proceed_keywords` 与
  `§3.3 resume_keywords` 在 `upload_conflict` 状态下的语义。
- `dialogue_act = reset`：接管 `/重新找` 等清空搜索条件的自然表达。

**优先级（phased-plan §2.5.5）**：显式斜杠命令 / 后端状态约束（`upload_conflict`
闭集词）> reducer 裁决 > LLM `dialogue_act` > keyword fallback。

阶段二**保留**关键词列表本身：

- `_is_cancel` / `proceed_keywords` / `resume_keywords` 三组不删，仅作 fallback；
- `_route_upload_conflict` 中闭集短语「继续发布 / 取消草稿 / 先找工人」是产品
  给定的有限选项集，**不属于「关键词膨胀」**，长期保留。

阶段四 primary 模式落地后，HIGH 项中**开放式中文兜底词表**全部删除（详见
phased-plan §4.1.3）。

### MEDIUM 项接管计划

`§2.3 _WORKER_SEARCH_SIGNALS / _JOB_POSTING_SIGNALS` 已在阶段一接入 worker 搜索护栏；
阶段二 dual_read 路径下由 reducer 配合 LLM 接管，但护栏仍保留作为**兜底**。
`_CITY_ADD_SIGNALS / _CITY_REPLACE_SIGNALS` 在仓库内为死代码，阶段二**不接入**也**不删除**，
留待阶段四统一删除（phased-plan §4.1.3）。

### 默认配置（产品确认）

- `dialogue_v2_mode = off`：代码 / 配置 / 测试就位，但生产仍走 legacy；
  上线后由 .env 切到 shadow / dual_read。
- `ambiguous_city_query_policy = clarify`：「北京有吗 + 已有西安」反问，不默认替换。
- `low_confidence_threshold = 0.6`：关键字段（city / job_category / salary_*）
  低于此置信度时强制 `needs_clarification=true`。

---

*本文档由对当前 main 分支代码的全量扫描生成。*
