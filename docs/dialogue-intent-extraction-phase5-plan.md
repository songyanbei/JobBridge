# 多轮会话意图识别与信息抽取：Phase 5 实施说明（结果感知对话策略）

日期：2026-05-10
基线提交：`7fd92d6`
配套文档：
- [dialogue-intent-extraction-phased-plan.md](dialogue-intent-extraction-phased-plan.md)（阶段一到阶段四主文档；本文是其 §Phase 5 的细化）
- [dialogue-intent-extraction-current-state.md](dialogue-intent-extraction-current-state.md)
- [dialogue-intent-extraction-stage3-acceptance.md](dialogue-intent-extraction-stage3-acceptance.md)
- [keyword-rules-audit.md](keyword-rules-audit.md)

---

## 0. 定位与前置依赖

### 0.1 Phase 5 解决什么问题

阶段一到阶段四把对话理解和状态裁决拆干净了，但**搜索结果出来之后系统该怎么继续推进对话**仍然散落在 [search_service.py](../backend/app/services/search_service.py) 和 [message_router.py](../backend/app/services/message_router.py) 里：

1. **0/低召回 fallback 是写死的级联**：[_run_job_fallback_steps](../backend/app/services/search_service.py) 只能"严格更优才采纳"地往下走，无法让用户先确认"是否要把薪资放宽 10%"，也无法根据用户上一轮明确说"工资不能再低了"跳过薪资放宽步骤。
2. **`show_more` 翻完文案是固定字符串**：当前 `已经是所有匹配结果了。要不要调整条件重新搜索？` 不区分到底建议换城市、换工种还是放宽薪资。
3. **软偏好字段（包吃、包住、班次）抽出来了但搜不到也排不动**：[slot_schema.py](../backend/app/dialogue/slot_schema.py) 已把这些字段标 `filter_mode=soft / ranking_weight=None`，reranker 仍只看 `query` 文本，schema 元数据没真正生效。
4. **结果感知的语义没有正式的裁决层**：阶段二 `DialogueDecision.post_search_action` 一直固定为 `'none'`，是为本阶段预留的位。

Phase 5 的目标：引入 `post_search_reducer` 作为**结果感知二阶段裁决**，把上述逻辑统一到声明式的纯函数里；让软偏好字段从"抽出来挂在 criteria 上"升级为"reranker 真消费"。

### 0.2 入场前置（开发期 vs 上线期）

**开发期硬依赖（必须满足才能动手写 Phase 5 代码）**：

| 依赖 | 状态 |
|---|---|
| `slot_schema` 中 `filter_mode` / `ranking_weight` 字段位 | ✅ [slot_schema.py:58-59](../backend/app/dialogue/slot_schema.py) |
| `DialogueDecision.post_search_action` 兼容预留位（固定 `'none'`） | ✅ [dialogue_reducer.py:94](../backend/app/services/dialogue_reducer.py) |
| Phase 4 primary 代码路径已接通（`classify_dialogue` primary 分支） | ✅ commit `88958e0` |
| reducer 是纯函数 + 声明式 `state_transition` | ✅ Phase 2 落地 |
| Reranker 抽象类已存在，签名稳定 | ✅ [base.py:180](../backend/app/llm/base.py) |
| `_run_job_fallback_steps` / `FallbackOutcome` 结构已稳定 | ✅ [search_service.py:570](../backend/app/services/search_service.py) |

**上线期门槛（Phase 5 任何子阶段进 primary 之前必须满足）**：

- 阶段四 primary 已 100% 灰度且稳定 ≥ 2 周；
- 阶段四关键指标（`empty_search_result_rate / upload_conflict_rate / avg_turns_to_search / fallback 率`）无回退；
- Phase 5 自身的 shadow 数据 ≥ 1 周（详见 §5.5）。

**重要约束**：开发期可以并行推 Phase 5 代码，但 Phase 5 任何子阶段的 `post_search_policy_mode` 默认必须保持 `off`，**不允许在阶段四 primary 未稳定前把结果感知逻辑接到主路径上**。

### 0.3 子阶段拆分

Phase 5 拆 5 个可独立 PR、独立灰度的子阶段：

```
5.0 接口契约 + DTO 预埋（不改主链路）
  └── 5.1 post_search_reducer 接通 + show_more 降级语义
         └── 5.2 0/低召回策略升级（auto_relax / suggest_relaxation / ask_clarification）
                └── 5.3 软偏好排序（Reranker 接口扩参 + ranking_weight 解锁）
                       └── 5.4 可见性文案 + 灰度推全
```

每个子阶段独立 PR、独立验收、独立灰度桶。任何子阶段关键指标回退立即回滚到上一个子阶段。

---

## 5.0 接口契约 + DTO 预埋

定位：先把所有结构和函数签名定下来，但默认行为完全等价当前主链路（`post_search_action='none'`）。这一步合入主分支后，对线上行为零影响，便于后续子阶段并行开发。

### 5.0.1 功能

1. **新 DTO `PostSearchDecision`**：
   ```python
   class PostSearchDecision(BaseModel):
       action: Literal[
           "show_results",          # 直接展示当前 candidates（默认）
           "show_results_with_soft_pref_notice",  # 展示 + 软偏好可见性文案前缀
           "auto_relax_and_retry",  # 自动放宽并重检索（与现有 fallback 一致）
           "suggest_relaxation",    # 不自动放宽，只给方向建议（与现有 0 结果建议一致）
           "ask_clarification",     # 反问用户某个维度（"是否可以放宽薪资"）
           "paginate_no_more",      # show_more 已翻完，给降级建议
           "no_action",             # reducer 决定不干预，message_router 走原路径
       ]
       relax_step: str | None = None       # auto_relax_and_retry 时指定 step 名
       clarification: dict | None = None   # ask_clarification 时的结构化文案
       suggested_directions: list[dict] = []  # suggest_relaxation / paginate_no_more 时给方向
       soft_pref_notice: str | None = None # show_results_with_soft_pref_notice 时的文案
       reasoning: str = ""                 # 调试用，不进回复
   ```

2. **新函数签名 `post_search_reduce`**：
   ```python
   def post_search_reduce(
       *,
       parse_result: DialogueParseResult,
       decision: DialogueDecision,
       session: SessionState,
       search_outcome: SearchOutcome,  # 见下文
       role: str,
   ) -> PostSearchDecision:
       """纯函数：基于搜索结果 + 阶段二裁决产出二阶段动作。

       不写 session、不调 LLM、不调 handler。所有副作用由 message_router/applier
       根据 PostSearchDecision 执行。
       """
   ```

3. **新 DTO `SearchOutcome`**：把当前散在 [search_service.py](../backend/app/services/search_service.py) `SearchResult` 里的"过程信息"暴露出来，方便 reducer 决策。
   ```python
   @dataclass
   class SearchOutcome:
       direction: Literal["search_job", "search_worker"]
       criteria_used: dict                    # 实际跑 SQL 的 criteria（含放宽后版本）
       initial_count: int                     # 原 criteria 下的硬过滤命中数（放宽前）
       final_count: int                       # 放宽 / fallback 后实际返回的候选数
       desired_count: int                     # 本次搜索期望返回的候选数（= top_n，由 search_service 注入）
       low_recall_threshold: int              # 触发 fallback 的阈值；当前 search_service 用 top_n 本身作为阈值
                                              # （即 initial_count < top_n 即视为低召回），5.2 reducer 复用此阈值保持等价
       applied_relax_step: str | None         # 已采纳的放宽步（None=未放宽）
       fallback_suggestions: list[FallbackSuggestion]  # 0 召回探查到的方向
       soft_pref_hits: dict[str, int]         # 候选集中各软偏好字段命中数（供 5.4 可见性文案使用）
       has_more: bool                         # 是否还有未展示候选（用于 show_more）
       snapshot_exhausted: bool               # show_more 调用且快照已翻完
   ```

4. **DTO 中立模块（避免循环 import）**：当前 [search_service.py:147](../backend/app/services/search_service.py) `FallbackSuggestion` 和 [search_service.py:140](../backend/app/services/search_service.py) `SearchResult` 都定义在 `search_service` 模块内部；`SearchOutcome.fallback_suggestions: list[FallbackSuggestion]` 又会被 `post_search_reducer` 导入消费——这条引用链如果让 `search_service.py` 反向 import `post_search_reducer.py` 就会形成循环。**5.0 子阶段**新建中立模块 `backend/app/schemas/search.py`（与现有 `backend/app/schemas/conversation.py` 同层），把 `SearchResult / FallbackSuggestion / FallbackOutcome / SearchOutcome` 四个搜索层 DTO 全部迁过去；`search_service.py` 改为从该模块 re-export 旧名（`from app.schemas.search import SearchResult, FallbackSuggestion, FallbackOutcome`，保持现有调用方零改动）；`post_search_reducer.py` 也只 import `app.schemas.search`，不 import `app.services.search_service`。`PostSearchDecision` 仍留在 `post_search_reducer.py`（属于对话层裁决产物，不是搜索层 DTO）。

5. **`PostSearchContext` 上下文 DTO（撑起 5.1/5.2 共用 applier 签名）**：当前 [dialogue_applier.py](../backend/app/services/dialogue_applier.py) `apply_decision(decision, session, *, msg, intent_result)` 是**会话状态 only** 的设计——拿不到 `db / user_ctx / raw_query`，也不返回 `ReplyMessage`。Phase 5 的 `post_search_applier` **不能复用** 这个签名风格，因为 5.2 的 `auto_relax_and_retry` 需要在 applier 内调用 `search_service.execute_relaxed_search(...)`，必须拿到 `db / user_ctx / raw_query`，且要产出 `ReplyMessage`。**5.0 子阶段**就把上下文 DTO 定义清楚，避免 5.1 立一个窄签名、5.2 再扩参导致契约断裂：

   ```python
   # backend/app/services/post_search_reducer.py 顶部（与 PostSearchDecision 同文件）
   @dataclass
   class PostSearchContext:
       """post_search_applier 的统一入参；5.1 仅用其中一部分，5.2 接 auto_relax 时
       消费 db/user_ctx/raw_query。一次性定型，避免后续子阶段反复改 applier 签名。"""
       decision: PostSearchDecision
       search_result: SearchResult
       search_outcome: SearchOutcome
       parse_result: DialogueParseResult     # 二阶段 reducer 在 5.2 复用
       dialogue_decision: DialogueDecision   # 二阶段 reducer 在 5.2 复用
       session: SessionState
       msg: WeComMessage                     # 用于 _reply(userid, ...) 构造
       user_ctx: UserContext
       db: Session                           # 5.2 execute_relaxed_search 必需
       raw_query: str                        # 5.2 二次 reranker 必需
       role: str                             # 软偏好 / 文案视角
       recursion_depth: int = 0              # 防 5.2 第二轮 reducer 死循环；硬上限=1
   ```

   5.1 子阶段的 applier 主入口确定为 `apply_post_search_decision(ctx: PostSearchContext) -> list[ReplyMessage]`（与 [_route_v2_resolve_conflict](../backend/app/services/message_router.py) 返回类型保持一致，方便上层拼接）。5.2 不再动签名，只在函数体内多处理 `auto_relax_and_retry / ask_clarification` 两个 action 分支。

6. **`DialogueDecision.post_search_action` 解锁 Literal 集合**：从 `Literal["none"]` 扩到包含上述 7 个动作；但 reducer 默认仍输出 `"none"`（由 `post_search_reduce` 替代输出）。
7. **`Settings.dialogue_policy.post_search_policy_mode`**：新增 `Literal["off", "shadow", "on"] = "off"`，默认 off。
8. **`Reranker.rerank` 接口扩参（仅声明，不消费）**：新增两个带默认值的可选参数 `soft_preferences: dict | None = None`、`ranking_weights: dict[str, float] | None = None`。本子阶段所有 provider 实现接收形参但忽略，确保后续子阶段引入实际消费时不破坏现有调用。

### 5.0.2 边界

- **不**改变任何 provider 的实际行为（reranker 入参扩展但不读）。
- **不**接通 `post_search_reduce`：本子阶段 message_router 仍按现有路径走，不调 reducer。
- **不**给软偏好字段填实际 `ranking_weight`（仍全部 None）。
- **不**改 search_service 内部 `_run_*_fallback_steps` 逻辑（5.2 子阶段才动）。
- **不**改 `show_more` 文案（5.1 子阶段才动）。
- **不**改 DB schema、不动 SQL 字段。

### 5.0.3 改动范围

| 文件 | 改动点 |
|---|---|
| `backend/app/schemas/search.py`（**新文件**，DTO 中立位） | 把 [search_service.py:140 `SearchResult`](../backend/app/services/search_service.py)、[search_service.py:147 `FallbackSuggestion`](../backend/app/services/search_service.py)、[search_service.py:159 `FallbackOutcome`](../backend/app/services/search_service.py) 三个 dataclass **整体迁移**到此文件；新增 `SearchOutcome` dataclass（结构见 §5.0.1 第 3 项）。模块仅依赖 stdlib + `app.config` 等基础包，**不**反向 import 任何 `app.services.*`。 |
| [backend/app/services/search_service.py](../backend/app/services/search_service.py) | 删除三个 DTO 的本地定义；改为 `from app.schemas.search import SearchResult, FallbackSuggestion, FallbackOutcome, SearchOutcome` + re-export（保持 `search_service.SearchResult` 旧引用名仍可用，避免改 [test_search_service.py](../backend/tests/unit/test_search_service.py) 等测试）。新增 `_build_search_outcome(...)` 返回 `SearchOutcome`。 |
| `backend/app/services/post_search_reducer.py`（**新文件**） | 定义 `PostSearchDecision` / `PostSearchContext` / `post_search_reduce` 骨架；骨架函数体直接 `return PostSearchDecision(action="no_action")`，让接通也是无副作用。**只 import `app.schemas.search`**，不 import `app.services.search_service`，避免循环依赖。 |
| [backend/app/services/dialogue_reducer.py](../backend/app/services/dialogue_reducer.py) | `DialogueDecision.post_search_action` Literal 集合从 `["none"]` 扩到完整集合；reducer 默认仍输出 `"none"`。 |
| [backend/app/llm/base.py](../backend/app/llm/base.py) | `Reranker.rerank` 抽象方法签名新增 `soft_preferences: dict \| None = None`、`ranking_weights: dict[str, float] \| None = None`（带默认值，向后兼容）。 |
| `backend/app/llm/providers/doubao.py` / `qwen.py` / 其他 provider | 同步形参签名，函数体接收但忽略；至少一个 mock provider 验证签名兼容。 |
| [backend/app/services/search_service.py](../backend/app/services/search_service.py) | **签名变更（一次性集中在 5.0 完成）**：`search_jobs / search_workers / show_more` 返回类型改为 `tuple[SearchResult, SearchOutcome]`；新增 `_build_search_outcome(...)` 构造 `SearchOutcome`（`SearchResult / SearchOutcome / FallbackSuggestion / FallbackOutcome` 此时已迁到 `app.schemas.search`，本文件改为 import）；`SearchResult` 自身字段和文案**完全不变**（包含 `show_more` 翻完时的兜底字符串「已经是所有匹配结果了。要不要调整条件重新搜索？」，由 search_service 继续直接产出，5.1/5.4 都不删）。 |
| [backend/app/services/message_router.py](../backend/app/services/message_router.py) | **同步更新所有调用方**：`_handle_show_more`（[message_router.py:1125](../backend/app/services/message_router.py)）、`_run_search`（[message_router.py:1578](../backend/app/services/message_router.py)）、`_handle_search`、`_handle_follow_up` 全部改为 `result, outcome = search_service.xxx(...)`，本子阶段**不消费** `outcome`（仅解构、即丢弃），保证逐字节等价旧路径。注意要 grep 全仓库 `search_jobs(` / `search_workers(` / `search_service.show_more(` 调用点，遗漏一处都会造成 `tuple` 误用为 `SearchResult` 的运行时错误。 |
| [backend/app/config.py](../backend/app/config.py) | `DialoguePolicy` 新增 `post_search_policy_mode: Literal["off","shadow","on"] = "off"`，配套 `_LEGACY_DIALOGUE_FIELD_MAP` 不需要扩（这是新字段，没有旧 env 名）。 |
| `backend/tests/unit/test_post_search_reducer.py`（**新文件**） | 单测：`post_search_reduce` 默认返回 `no_action`；DTO 序列化兼容；`SearchOutcome` 在各路径下字段齐全。 |
| `backend/tests/unit/test_dialogue_reducer.py` | 补一条单测：`DialogueDecision.post_search_action` 接受新 Literal 值且默认仍是 `"none"`。 |
| `backend/tests/unit/test_search_service.py` | 补单测：`search_jobs / search_workers / show_more` 产出的 `SearchOutcome` 字段在 0 召回 / 放宽 / 翻完 三类场景下都齐全且字段语义正确；返回值是 `tuple` 且第 0 位的 `SearchResult.reply_text` 与 5.0 前的旧实现逐字节相等。 |
| `backend/tests/unit/test_message_router.py` | 补单测：所有解构调用点（`_handle_show_more / _run_search / _handle_search / _handle_follow_up`）能正确解 tuple；本子阶段下 `outcome` 丢弃后 reply 与旧路径完全相同。 |

### 5.0.4 验收条件

1. 全量 pytest 通过；现有 7+ 条 dialogue golden case **行为完全不变**（diff 对照阶段四主链路输出）。
2. `Settings()` 默认实例化下 `dialogue_policy.post_search_policy_mode == "off"`，且新字段不出现在任何旧 fixture/conftest 的硬编码 dict 里（验证向后兼容）。
3. `post_search_reduce(...)` 在所有合法入参下返回 `PostSearchDecision(action="no_action")`，纯函数无副作用（无 session 写入、无外部调用）。
4. Reranker 各 provider 在不传新参数时调用结果**逐字节等价**于本子阶段前（用 fixture 录制对比）。
5. **签名变更收口**：grep 全仓库 `search_service.search_jobs(` / `search_service.search_workers(` / `search_service.show_more(` 调用点，所有命中都是 tuple 解构形式；`SearchResult.reply_text` 在 50 条历史会话回放下与基线提交逐字节相等（验证签名变更没有改变文案）。
6. **DTO 中立位无循环依赖**：grep `app.schemas.search` 模块的 import 列表，**不出现**任何 `from app.services.` 或 `import app.services.` 语句；反向 grep `app.services.search_service` / `app.services.post_search_reducer` 都改为 `from app.schemas.search import ...`，确认双向引用都收口到中立模块。`python -c "import app.schemas.search; import app.services.search_service; import app.services.post_search_reducer"` 在干净进程里能成功导入，不报循环。
7. **`PostSearchContext` 字段齐全**：5.1 的 `apply_post_search_decision(ctx)` 在所有 5.1 路径（`no_action / paginate_no_more / ask_clarification` 桩）下访问 `ctx.decision / ctx.search_result / ctx.search_outcome / ctx.session / ctx.msg` 不抛 `AttributeError`；`db / user_ctx / raw_query / role` 字段在 5.1 暂不被消费但必须能被构造（不允许为 `None`，由 message_router 透传）。

---

## 5.1 post_search_reducer 接通 + show_more 降级语义

定位：把 `post_search_reduce` 真正接到 message_router，但仅启用最低风险的一条路径：**`show_more` 翻完时给具体降级建议**。0/低召回策略（5.2）、软偏好排序（5.3）和软偏好可见性文案（5.4）继续走旧路径。

> **设计修订（v2）**：原 v1 计划在 5.1 同时接通"软偏好可见性文案"，但当时 reranker 尚未按 `ranking_weight` 排序（要等到 5.3），文案承诺的"已优先展示"语义在 5.1 实际并未发生。为避免向用户做尚未兑现的承诺，可见性文案整体推迟到 5.4（5.3 接通排序之后）。5.1 仅做 `paginate_no_more` 的真实接通和 `ask_clarification` 渲染桩。

### 5.1.1 功能

1. **接通点**：[message_router.py](../backend/app/services/message_router.py) `_handle_search` / `_handle_follow_up` / `_handle_show_more` 在调用 `search_service.search_jobs / search_workers / show_more` 拿到 `(SearchResult, SearchOutcome)` 后（签名变更已在 5.0 完成），按 `post_search_policy_mode` 分流：
   - `off`：不调 reducer，直接使用 `SearchResult.reply_text` 作为最终回复（保持 5.0 完工后逐字节等价）。
   - `shadow`：调 reducer 拿 `PostSearchDecision`，**只写日志**（`post_search_decision` 事件），不影响实际 reply（仍用 `SearchResult.reply_text`）。
   - `on`：调 reducer，按 `PostSearchDecision.action` 决定是否覆盖 `SearchResult.reply_text`：
     - `no_action` → 不覆盖，沿用 `SearchResult.reply_text`；
     - `paginate_no_more` → applier 用 `slot_schema.relaxation_directions(...)` 渲染新文案覆盖 reply；
     - 其他 action 在本子阶段 reducer 不输出（5.2/5.3/5.4 才接通）。

2. **`show_more` 降级语义升级（仅在 `on` 模式生效）**：search_service 内部 `show_more` 翻完时**仍输出旧字符串**「已经是所有匹配结果了。要不要调整条件重新搜索？」（不动 search_service）；on 模式下由 reducer + applier 检测 `SearchOutcome.snapshot_exhausted=True` 并覆盖 reply 为更具体的建议方向：
   - 仅有 `city + job_category` → 建议放宽薪资 / 换附近城市 / 切换工种大类。
   - 已有 `salary_floor_monthly` → 建议下调薪资 10% / 换城市。
   - 已有 `salary_floor_monthly + 软偏好` → 建议先去掉软偏好。
   - 输出 `PostSearchDecision(action="paginate_no_more", suggested_directions=[...])`，applier 渲染为模板文案后**覆盖** `SearchResult.reply_text`。
   - off / shadow 模式下 `SearchResult.reply_text` 仍是旧字符串，外部用户不可感知差异。

3. **结构化反问字段（埋桩，不启用）**：`PostSearchDecision.action="ask_clarification"` 在本子阶段不会被 reducer 输出（产出条件留到 5.2），但 applier 必须能正确渲染（生成与现有 clarification 同构的 `ReplyMessage`）。本子阶段单测用桩输入直接构造 `PostSearchDecision(action="ask_clarification", ...)`，验证 applier 渲染正确，不经过 reducer。

### 5.1.2 边界

- **不**改变 0/低召回路径（`auto_relax_and_retry / suggest_relaxation` 不输出）—— 留 5.2。
- **不**改 reranker 实际排序行为 —— 留 5.3。
- **不**输出 `show_results_with_soft_pref_notice` —— 推迟到 5.4，等 5.3 真正接通排序后再让 reducer 输出。本子阶段 applier 中**不**实现该 action 的渲染分支（如收到该 action 视为 fallback 到 `no_action`，记录 `post_search_unsupported_action` 日志）。
- **不**让 reducer 写 session：所有 session 状态变更（如清快照、记埋点）由 applier 执行；reducer 仅产出声明式 `PostSearchDecision`。
- **不**新增中文关键词列表来识别"调整条件"等用户表达 —— 由阶段二 `dialogue_act` 接管（违反 §跨阶段共同约束 7 视为不达标）。
- **不**改 `SearchResult` 数据类的现有字段（含 search_service 内 `show_more` 翻完兜底文案）—— off / shadow 模式必须能继续用 `SearchResult.reply_text` 直出。

### 5.1.3 改动范围

| 文件 | 改动点 |
|---|---|
| `backend/app/services/post_search_reducer.py` | 实现 `_decide_paginate_no_more(...)` 一条分支；其他 action 仍返回 `no_action`。 |
| `backend/app/services/post_search_applier.py`（**新文件**） | `apply_post_search_decision(ctx: PostSearchContext) -> list[ReplyMessage]`（签名已在 5.0 §5.0.1 第 5 项确定，5.1 不再调整）：根据 `ctx.decision.action` 决定是否覆盖 `ctx.search_result.reply_text`。本子阶段实现 `no_action`（直出 `ctx.search_result.reply_text` 包成 `[ReplyMessage]`）/ `paginate_no_more`（用 `slot_schema.relaxation_directions(ctx.session.search_criteria, frame)` 渲染覆盖）/ `ask_clarification`（覆盖，渲染桩，参数从 `ctx.decision.clarification` 读）三条；其他 action 走"未实现 → fallback `no_action` + 日志事件 `post_search_unsupported_action`"。返回类型与 [_route_v2_resolve_conflict](../backend/app/services/message_router.py) 对齐为 `list[ReplyMessage]`，便于上层 message_router 直接拼接。 |
| [backend/app/services/message_router.py](../backend/app/services/message_router.py) | `_handle_search / _handle_follow_up / _handle_show_more` 接 `post_search_reduce` 三模式分流；shadow 模式打 `post_search_decision` 日志事件（含 `action / reasoning / would_be_reply_diff`）；on 模式构造 `PostSearchContext(decision=..., search_result=..., search_outcome=..., parse_result=..., dialogue_decision=..., session=session, msg=msg, user_ctx=user_ctx, db=db, raw_query=msg.content, role=user_ctx.role, recursion_depth=0)` 并调 `apply_post_search_decision(ctx)` 拿最终 reply。`PostSearchContext` 的全部字段必须由 message_router **构造时填齐**，不允许在 applier 内补填——这是为 5.2 二次检索预留的契约。 |
| [backend/app/services/search_service.py](../backend/app/services/search_service.py) | **不改返回签名**（5.0 已完成）；**不改** `show_more` 翻完文案（保留 `SearchResult.reply_text="已经是所有匹配结果了..."` 作为 off / shadow 兜底）。仅补：`SearchOutcome.snapshot_exhausted` 字段在 show_more 翻完时为 `True`，供 reducer 判定。 |
| [backend/app/dialogue/slot_schema.py](../backend/app/dialogue/slot_schema.py) | 新增 helper `relaxation_directions(criteria, frame) -> list[dict]`，根据 criteria 形态产出可建议的放宽方向（列表，含 `dimension / hint_text / target_field`），供 `_decide_paginate_no_more` 复用。 |
| `backend/tests/unit/test_post_search_reducer.py` | 扩 3 类单测：`paginate_no_more` 三种 criteria 形态各一条；shadow 模式不影响 reply（fixture 录制对比）；`ask_clarification` 渲染（applier 端，桩输入直接构造 decision）。 |
| `backend/tests/fixtures/dialogue_golden/` | 新增 2 条 golden：worker 翻完 → 建议放宽薪资 / 换附近城市；broker 翻完 → 建议换工种大类。**不**新增软偏好可见性文案 golden（推迟到 5.4）。 |

### 5.1.4 验收条件

1. **`off` 模式逐字节等价 5.0**：用 record/replay 比较 50 条历史会话回放结果，所有 reply 完全相同（含 show_more 翻完仍是旧字符串）。
2. **`shadow` 模式 50 条会话回放**：reply 与 `off` 相同；`post_search_decision` 日志事件齐全（含 `action / reasoning / would_be_reply_diff`）。
3. **`on` 模式 golden case 全绿**：2 条新 golden + 阶段二/三/四 7+ 条历史 golden 全部通过；其中历史 golden 中"翻完结果"分支断言在 `on` 模式下升级为根据 criteria 形态的具体建议；off / shadow 模式断言仍是旧字符串（同一 golden 文件按 mode 分支断言，避免预期漂移）。
4. **回滚演练**：`post_search_policy_mode=on` → `off`，下一轮请求立即回旧路径，session 无残留状态错乱。
5. **降级文案不依赖 LLM**：`paginate_no_more` 文案完全由模板生成（用 `slot_schema.relaxation_directions(...)` + 静态模板），与 §跨阶段共同约束 4 一致。
6. **未启用 action 防御**：reducer 输出 `show_results_with_soft_pref_notice` 等本子阶段未实现的 action 时，applier fallback 到 `no_action` + 日志告警；单测验证此防御路径。

---

## 5.2 低召回 / 0 结果策略升级

定位：把 [search_service.py](../backend/app/services/search_service.py) 现有 `_run_job_fallback_steps / _run_resume_fallback_steps` 的"无脑级联放宽"改造为 **reducer 决定走哪一步**。让用户能在自动放宽前被询问、能根据上一轮发言跳过某个放宽步骤。

> **触发口径修订（v3）**：v2 写"`SearchOutcome.initial_count == 0` 时由 reducer 选择策略"，但当前 search_service 的 fallback 触发口径是 `len(candidates) < top_n`（参见 [search_service.py:193](../backend/app/services/search_service.py)），覆盖 1~2 条这种**低召回**场景。如果 5.2 只接管 0 结果，1~2 条低召回会丢掉现有自动放宽行为，与 §5.2.4 验收 #2"默认行为分支逐字节等价当前"冲突。本子阶段统一改为 **`initial_count < low_recall_threshold`** 触发（默认 `low_recall_threshold = top_n`，与当前 search_service 保持一致），由 5.0 引入的 `SearchOutcome.low_recall_threshold` 字段携带阈值进入 reducer。

### 5.2.1 功能

1. **职责拆分**：
   - search_service 仍负责"跑某个 criteria 的 SQL"和"探查低召回时哪些方向有结果"；但**不再自己决定是否采纳放宽结果**。
   - `post_search_reduce` 拿到 `SearchOutcome.initial_count < SearchOutcome.low_recall_threshold` 时（覆盖 0 结果与 1~2 条低召回），根据策略选择：
     - `auto_relax_and_retry`：自动采纳放宽（与当前行为兼容；`relax_step` 指定步骤）；
     - `suggest_relaxation`：把方向给用户、不自动放宽（与当前 `_probe_*_suggestions` 兼容）；
     - `ask_clarification`：用 5.1 埋好的 `ask_clarification` 渲染反问（"找不到符合条件的岗位，要把薪资放宽 10% 重新搜索吗？"）。

2. **决策依据（明确写死，不留隐式规则；只用现有 Phase 2 契约可读取的信号）**：
   - **当前 turn 的 `accepted_slots_delta` 包含 relax 步骤将要覆盖的字段** → 跳过对应维度的 auto_relax，改 `ask_clarification`。例：用户当前 turn 把 `salary_floor_monthly` 从未设置改为 5000，但 5000 检索 0 命中；不应该立刻又把 5000 偷偷放宽为 4500，而要反问"5000 找不到，要放宽到 4500 重新搜索吗？"。这条规则**只看本 turn 的 slots_delta**，不依赖历史 turn 的语义记忆。
   - 用户处于 `awaiting_fields` 中（仍在补槽）→ 不放宽，等用户补齐再搜；
   - 当前 turn 的 `confidence < low_confidence_threshold` → 不自动放宽，走 `suggest_relaxation`；
   - 其余默认行为：保持当前 [_run_*_fallback_steps](../backend/app/services/search_service.py) 三步级联（薪资 → 大类 → 去可选硬过滤），**等价当前线上行为**。
   - 决策表完整规则放在 [post_search_reducer.py](../backend/app/services/post_search_reducer.py) 顶部注释，每条规则配单测。
   - **未实现的语义记忆**：v1 计划中"用户上一轮明确表达过『工资不能再低了』"这类**跨 turn 硬约束记忆**需要在阶段二 `DialogueParseResult.dialogue_act` 中新增 `set_constraint` 枚举值并由 reducer 持久化到 session，本文档不引入该机制；如未来需要，由独立 PR 扩 Phase 2 DTO + prompt + session schema 后再补这条规则。Phase 5 只用"当前 turn 用户刚断言的字段"作为护栏。

3. **`SearchOutcome` 扩展**：新增 `available_relax_steps: list[str]`（search_service 探查后告诉 reducer "可以走 relax_salary_10pct 或 broaden_job_category"），由 reducer 选择具体走哪一步。search_service 不再自己 for 循环采纳。

4. **用户确认放宽的二次检索（签名 + 输入语义）**：`PostSearchDecision(action="auto_relax_and_retry", relax_step="relax_salary_10pct")` 由 applier 调 `search_service.execute_relaxed_search(...)` 拿到放宽后结果。**完整签名**（与现有 [search_jobs](../backend/app/services/search_service.py) 链路一致，rerank / `save_snapshot` / `record_shown` / 权限过滤都依赖这些上下文）：

   ```python
   def execute_relaxed_search(
       original_criteria: dict,                  # 用户主搜索时的原始 criteria（未放宽版本）
       step: str,                                # 放宽步名，如 "relax_salary_10pct" / "broaden_job_category" /
                                                  # "drop_optional_filters"，由 search_service 内部据此计算放宽后的 criteria
       *,
       direction: Literal["search_job", "search_worker"],  # 决定走 _query_jobs 还是 _query_resumes
       raw_query: str,                           # 复用主搜索 raw_query，用于 reranker
       session: SessionState,                    # 写快照 / record_shown 必需
       user_ctx: UserContext,                    # 权限过滤 + role 必需
       db: Session,                              # SQL 执行 + reranker provider 加载必需
       user_msg_id: str | None = None,           # 透传给 _rerank_with_logging 做归因（与主搜索一致）
   ) -> tuple[SearchResult, SearchOutcome]:
       ...
   ```

   **关键输入语义**：第一参数**必须**是 `original_criteria`（未放宽版本），由函数内部根据 `step` **一次性**计算放宽后的 criteria。**不允许**直接传 `relaxed_criteria` 进来——否则若 reducer 在第一阶段已按 `step=relax_salary_10pct` 算出 4500，applier 二次调用又传 `relaxed_criteria={salary_floor_monthly: 4500}` + `step=relax_salary_10pct`，函数内部会再放宽一次到 4050（薪资被二次放宽）。这是 P1 评审里点名要避免的反模式。

   拿到 `(SearchResult, SearchOutcome)` 后，再以该结果走一次 reducer（**最多一次二阶段**，避免无限套娃；reducer 在第二轮统一输出 `show_results` 或 `suggest_relaxation`，由代码 assert 守护，见 §跨阶段共同约束 8）。

   **不允许的反模式**：
   - applier 自己拼一个空壳 `SearchOutcome` 当输入；签名必须由 search_service 一次性同时产出，确保 `initial_count / low_recall_threshold / fallback_suggestions / soft_pref_hits` 等字段语义在二阶段也完整。
   - 把 `relaxed_criteria` 当 `original_criteria` 传给 `execute_relaxed_search`（二次放宽风险，见上）。
   - 在 search_service 外部（applier / message_router）自行调 `_relax_step_compute(...)` 计算放宽 criteria —— 放宽算法是 search_service 内部细节，不暴露。

5. **跨 turn 放宽确认状态（独立于 upload_conflict 流程）**：当 reducer 输出 `ask_clarification` 反问"要把薪资放宽 10% 吗"时，applier 在写回 reply 前同时把待确认上下文写入 **新增的** `SessionState.pending_relaxation` 字段：
   ```python
   pending_relaxation: dict | None = None
   # 结构示例：
   # {
   #   "frame": "job_search",
   #   "direction": "search_job",         # 二次检索时透传给 execute_relaxed_search 的 direction
   #   "step": "relax_salary_10pct",
   #   "original_criteria": {...},        # 主搜索时的未放宽 criteria，确认后作为 execute_relaxed_search 第一参数
   #   "relaxed_criteria": {...},         # 仅用于反问文案展示与审计日志，不参与二次检索
   #   "raw_query": "西安饭店服务员 5000",  # **持久化主搜索 raw_query**：确认轮用户回"好的"，
   #                                       # 不能拿"好的"做 reranker query；二次 reranker 必须复用主搜索原文
   #   "user_msg_id": "msg_xxx",          # 主搜索时的 msg_id，二次 _rerank_with_logging 透传作归因
   #   "expires_at": "2026-05-10T12:00:00Z",
   # }
   ```
   **写入时机**：在主搜索 turn 内，`post_search_applier.apply_post_search_decision(ctx)` 走到 `ask_clarification` 分支时，把 `ctx.raw_query`（主搜索原文）和 `ctx.msg.msg_id` 一并写入 `pending_relaxation`，与 `original_criteria / step` 同时持久化。
   下一轮用户回应时由阶段二解析为新增 `dialogue_act=respond_relaxation_offer`，配套字段 `relaxation_response: Literal["accept","reject"]`。reducer 据此把 `state_transition` 设为 `apply_relaxation` / `cancel_relaxation` / `clear_pending_relaxation`。

   **执行归属（严格区分会话状态层与搜索动作层，模仿现有 [_route_v2_resolve_conflict](../backend/app/services/message_router.py) 的 short-circuit 模式）**：

   | 执行点 | 职责 | 拿到的上下文 |
   |---|---|---|
   | [dialogue_applier.apply_decision](../backend/app/services/dialogue_applier.py) | **仅** `clear_pending_relaxation`（session-only，把 `pending_relaxation` 置 None）。`apply_relaxation` / `cancel_relaxation` **不**在此处理——这两个 transition 需要 `db / user_ctx / raw_query` 才能跑二次检索，但当前 `apply_decision` 签名 `(decision, session, *, msg, intent_result)` 拿不到这些上下文。**额外约束**：apply_decision 内对 `apply_relaxation` / `cancel_relaxation` 显式声明为 no-op 分支（不走 unknown 兜底告警），避免误调时打 warning 干扰日志。但**调用方不应依赖该 no-op 行为**——见下一格 message_router 的调用规则。 | session 写入 only；不发回复 |
   | [message_router._handle_text](../backend/app/services/message_router.py) | **新增 short-circuit 分支**（位置：与现有 `if decision.dialogue_act == "resolve_conflict"` 平行，在通用分发前）：当 `decision.dialogue_act == "respond_relaxation_offer"` 时按 `state_transition` 分支调用：**(a)** 仅当 `state_transition == "clear_pending_relaxation"` 时调 `apply_decision(decision, session, msg=msg, intent_result=intent_result)` 物化清状态；**(b)** `apply_relaxation` / `cancel_relaxation` **跳过 apply_decision**，直接调 `_route_v2_relaxation_response(...)`（pending_relaxation 的清理由 route 函数自己负责）。无论哪条分支，最后都 `return` 该函数返回的 ReplyMessage 列表，**不**让该 dialogue_act 落入普通 chitchat / command / search 路由。 | `db / user_ctx / msg / session` 齐全 |
   | `message_router._route_v2_relaxation_response`（**新增**） | 1) 读 `session.pending_relaxation` 拿到 `direction / step / original_criteria / raw_query / user_msg_id`（**`raw_query` 必须从 pending 读取，不能用 `msg.content`**——确认轮用户消息通常是"好的 / 可以"，拿它做 reranker query 会让二次检索排序退化；**`original_criteria` 不读 `relaxed_criteria`**，`relaxed_criteria` 字段仅用于反问文案展示和审计日志，不再作为二次检索入参）；2) 根据 `decision.state_transition` 分支：`apply_relaxation` → 调 `search_service.execute_relaxed_search(pending["original_criteria"], pending["step"], direction=pending["direction"], raw_query=pending["raw_query"], session=session, user_ctx=user_ctx, db=db, user_msg_id=pending["user_msg_id"])` 拿 `(SearchResult, SearchOutcome)`，再以新 outcome 构造 `PostSearchContext(recursion_depth=1, ...)`（`PostSearchContext.raw_query` 也用 `pending["raw_query"]`，与 execute_relaxed_search 一致）走一次 `post_search_reduce` + `apply_post_search_decision(...)`；`cancel_relaxation` → 用模板渲染"好的，那我们换其他条件"；3) **执行后函数自身**清 `session.pending_relaxation = None`（不依赖 apply_decision 物化）。 | `db / user_ctx / msg / session` 齐全 |

   **不允许的反模式**：
   - 把 `apply_relaxation` 的搜索调用塞进 `dialogue_applier.apply_decision`（拿不到 `db`）；
   - 把 `apply_relaxation` 的搜索调用塞进 `post_search_applier.apply_post_search_decision`（该 applier 仅在搜索后链路里运行，user 回 "可以" 这一轮**还没**搜索；`post_search_applier` 只在 `_route_v2_relaxation_response` 拿到二次检索结果**之后**被调一次）；
   - 不接 short-circuit 直接让 `respond_relaxation_offer` 走通用 dispatch——会被路由到 chitchat / command / 普通 search，pending_relaxation 永远清不掉。

   **明确不复用现有结构**：
   - **不**复用 `SessionState.pending_interruption`：该字段在 [conversation.py:56](../backend/app/schemas/conversation.py) 是 upload_conflict 专用的瘦身意图载体，[message_router.py:_route_v2_resolve_conflict (line 754)](../backend/app/services/message_router.py) 已按此前提硬编码分发，混用会让搜索放宽与上传冲突缠在一起。
   - **不**复用 `SessionState.last_intent`：该字段是观测字段，不参与路由。
   - **不**复用 `dialogue_act=resolve_conflict`：[prompts.py:290](../backend/app/llm/prompts.py) 把 `resolve_conflict` 限定在 `active_flow=upload_conflict` 上下文，[message_router.py:_route_v2_resolve_conflict (line 754)](../backend/app/services/message_router.py) 也按这个前提分发；放宽确认是独立流程，需要独立 dialogue_act + 独立 short-circuit 分支。
   - **不**复用 `conflict_action`：放宽确认有自己的 `relaxation_response: Literal["accept","reject"]` 字段。
   - **不**复用 `_route_v2_resolve_conflict` 函数体：两条流程**结构同构但状态字段独立**，复用代码会重新引入 P1 评审里的耦合风险。

6. **关键词不接管**：用户回"好的 / 可以 / 放宽吧"由阶段二 `dialogue_act=respond_relaxation_offer` + `relaxation_response=accept` 接管。**不**新增中文关键词列表识别"放宽 / 调整 / 同意"。仅当 LLM 未识别且 `pending_relaxation` 非空时，message_router 用闭集兜底（"取消 / 不要 / 算了" → reject；"好 / 可以 / 行" → accept），与 [keyword-rules-audit.md](keyword-rules-audit.md) 中"系统提示中的闭集选项"分类一致，不属于开放词表。

### 5.2.2 边界

- **不**改 SQL 字段、**不**改 fallback 步骤本身的算法（仍是薪资 10% / 大类拓宽 / 去可选硬过滤三步）。
- **不**让 reducer 跑 SQL：reducer 只决定"走哪步"，SQL 由 search_service 在 applier 调用下执行。
- **不**支持任意多轮放宽：每次用户主搜索 → 最多一次自动放宽 + 一次反问确认，避免对话失控。
- **不**给用户暴露 fallback 内部步骤名（如 `drop_optional_filters`），文案统一为业务语义（"放宽薪资"、"放宽工种范围"）。

### 5.2.3 改动范围

| 文件 | 改动点 |
|---|---|
| [backend/app/services/search_service.py](../backend/app/services/search_service.py) | **拆分**：原 `_run_job_fallback_steps` 拆成 `_probe_relax_steps(criteria) -> list[(step_name, relaxed_criteria, count)]` 和 `execute_relaxed_search(original_criteria, step, *, direction, raw_query, session, user_ctx, db, user_msg_id=None) -> tuple[SearchResult, SearchOutcome]`（**完整签名见 §5.2.1 第 4 项**；与 `search_jobs / search_workers` 链路对齐：内部按 step 一次性计算放宽 criteria → `_query_jobs/_query_resumes` → reranker → `save_snapshot` → 权限过滤 → `_build_search_outcome`，**不**依赖外部传入 `relaxed_criteria`）；旧函数 `_run_*_fallback_steps` 内部改为调这两个 + 默认策略，保持向后兼容。新增 `SearchOutcome.available_relax_steps`、`SearchOutcome.relax_probe_results`（含每步候选数）。 |
| `backend/app/services/post_search_reducer.py` | 实现 `_decide_zero_result(...)` 决策表；保留默认行为分支（等价旧 fallback）。决策表只读取以下信号：当前 turn `accepted_slots_delta`、`session.awaiting_fields`、`parse_result.confidence`、`session.pending_relaxation`；**不**读取任何"历史 turn 的 dialogue_act 记忆"。 |
| `backend/app/services/post_search_applier.py` | 在 5.1 已稳定的 `apply_post_search_decision(ctx: PostSearchContext)` 入口内实现两个新 action：(a) `auto_relax_and_retry` → 调 `search_service.execute_relaxed_search(ctx.search_outcome.criteria_used, ctx.decision.relax_step, direction=ctx.search_outcome.direction, raw_query=ctx.raw_query, session=ctx.session, user_ctx=ctx.user_ctx, db=ctx.db, user_msg_id=ctx.msg.msg_id)` 拿 `(SearchResult, SearchOutcome)`——**注意**：此分支下 `ctx.search_outcome.criteria_used` **就是** original criteria（auto_relax 触发时主搜索这一轮还未放宽，criteria_used = 用户原始 criteria，与"用户确认放宽"路径里的 `pending_relaxation["original_criteria"]` 语义一致）；以新 outcome 构造 `PostSearchContext(recursion_depth=1, raw_query=ctx.raw_query, ...)`，再调一次 `post_search_reduce(...)` + 自身（递归一层，受 `assert ctx.recursion_depth <= 1` 守护），把结果包成 `list[ReplyMessage]` 返回；(b) `ask_clarification` → **持久化反问上下文**：写入 `ctx.session.pending_relaxation = {direction: ctx.search_outcome.direction, step: ctx.decision.relax_step, original_criteria: ctx.search_outcome.criteria_used, relaxed_criteria: <按 step 计算的展示用值>, raw_query: ctx.raw_query, user_msg_id: ctx.msg.msg_id, expires_at: ...}`（结构与 §5.2.1 第 5 项对齐，`raw_query / user_msg_id` 持久化是 P1 #2 评审硬要求）+ 渲染反问文案覆盖 reply。**注意**：本 applier **不**处理 `apply_relaxation / cancel_relaxation` 这两个 state_transition——这些是用户回应"可以 / 取消"那一轮才出现，归 `_route_v2_relaxation_response` 管。 |
| [backend/app/services/dialogue_applier.py](../backend/app/services/dialogue_applier.py) | 在现有 `apply_decision` 内新增对**三个** state_transition 的处理：(1) `clear_pending_relaxation` → 一行 `session.pending_relaxation = None`（与现有 `clear_awaiting` 同款）；(2) `apply_relaxation` / `cancel_relaxation` → **显式 no-op 分支**（`pass` + 日志事件 `dialogue_applier_relaxation_passthrough`，**不**走 unknown 兜底告警）。这两个 transition 真正的执行在 `_route_v2_relaxation_response`，applier 层面留 no-op 是为了在调用方（如 message_router）误调时不打 warning，但**正确的调用方应该按 §5.2.1 第 5 项规则跳过 apply_decision**，no-op 仅作防御层。 |
| [backend/app/schemas/conversation.py](../backend/app/schemas/conversation.py) | **新增** `SessionState.pending_relaxation: dict \| None = Field(default=None)`，TTL 复用 `dialogue_policy.search_awaiting_ttl_seconds`（不新增配置项）。结构见 §5.2.1 第 5 项。旧 Redis session 反序列化兼容（默认 None），按现有阶段一同款 `Field(default=None)` 风格。 |
| [backend/app/services/dialogue_reducer.py](../backend/app/services/dialogue_reducer.py) | `DialogueParseResult.dialogue_act` Literal **新增** `respond_relaxation_offer`；新增配套字段 `relaxation_response: Literal["accept","reject"] \| None = None`（仅当 `dialogue_act=respond_relaxation_offer` 时非 None，其他情况强制为 None，由 Pydantic validator 守护）。`DialogueDecision.state_transition` Literal 新增 `apply_relaxation / cancel_relaxation / clear_pending_relaxation`。**不**复用 `resolve_conflict / conflict_action`。 |
| [backend/app/llm/prompts.py](../backend/app/llm/prompts.py) | `DIALOGUE_PARSE_PROMPT_V2` 新增 `respond_relaxation_offer` 的 few-shot（独立段落，与 `resolve_conflict` 平行）；prompt 中明确触发上下文："系统上一轮反问『要把 X 放宽吗』、且 `session.pending_relaxation` 非空"，避免与 `resolve_conflict` 的 `upload_conflict` 上下文混淆。 |
| [backend/app/services/message_router.py](../backend/app/services/message_router.py) | **(1) `_handle_text` 新增 short-circuit 分支**：在现有 `if decision.dialogue_act == "resolve_conflict"` 同位置（与之并列），新增 `if decision.dialogue_act == "respond_relaxation_offer"` 分支，按 `state_transition` 决定是否调 apply_decision —— **仅 `clear_pending_relaxation` 时调** `apply_decision(decision, session, msg=msg, intent_result=intent_result)`；`apply_relaxation` / `cancel_relaxation` **跳过** apply_decision。然后无条件调 `_route_v2_relaxation_response(decision, msg, user_ctx, session, db)` 返回 ReplyMessage 列表并 return。**(2) 新增 `_route_v2_relaxation_response(decision, msg, user_ctx, session, db) -> list[ReplyMessage]`**（与 [_route_v2_resolve_conflict (line 754)](../backend/app/services/message_router.py) 平行，**不**复用其函数体）：从 `session.pending_relaxation` 读 `original_criteria + step + direction + raw_query + user_msg_id`（**完整字段集见 §5.2.1 第 5 项**；不读 `relaxed_criteria`，只读 `original_criteria` 避免二次放宽）。按 `decision.state_transition` 分支处理 `apply_relaxation`（调 `search_service.execute_relaxed_search(pending["original_criteria"], pending["step"], direction=pending["direction"], raw_query=pending["raw_query"], session=session, user_ctx=user_ctx, db=db, user_msg_id=pending.get("user_msg_id"))` + 构造 `PostSearchContext(recursion_depth=1, raw_query=pending["raw_query"], ...)` + 调 `post_search_applier`）/ `cancel_relaxation`（用模板渲染"好的，那我们换其他条件"），**函数内部**在执行后置 `session.pending_relaxation = None`（不依赖 apply_decision 物化）。**注意**：本轮 `msg.content`（用户回的 "好的 / 可以 / 取消 / 算了"）**仅用于本轮意图确认**（在 reducer 上游解析为 `respond_relaxation_offer + relaxation_response`），**绝不**作为二次 reranker 的 query；二次检索的 `raw_query` 必须从 `pending_relaxation` 读取主搜索原文，否则 reranker 拿到 "好的" 排序退化（P1 #2 评审硬约束，见 §5.2.4 验收 #8）。**(3) 闭集兜底**：仅在 `pending_relaxation` 非空 + LLM 未识别为 `respond_relaxation_offer` 时，在 short-circuit 之前用闭集匹配（"取消 / 不要 / 算了 → reject、好 / 可以 / 行 → accept"）合成一个 `DialogueDecision` 进入 short-circuit。 |
| [backend/app/dialogue/slot_schema.py](../backend/app/dialogue/slot_schema.py) | 新增 helper `relax_step_human_label(step) -> str`，把内部 step 名映射为业务文案。 |
| `backend/tests/unit/test_post_search_reducer.py` | 扩 0 结果决策表全覆盖单测：当前 turn slots_delta 触碰 / awaiting / 低置信度 / 默认四种入口各 ≥ 2 条；冲突优先级显式断言。 |
| `backend/tests/unit/test_dialogue_reducer.py` | 新增 `respond_relaxation_offer` 解析单测；新增"`respond_relaxation_offer` 在 `pending_relaxation=None` 上下文产出时 reducer 视为 LLM 误判 → 降级 chitchat"的防御单测。 |
| `backend/tests/fixtures/dialogue_golden/` | 新增 4 条 golden：(a) 0 结果默认自动放宽薪资 → applier 直出（不反问），等价旧 fallback；(b) 0 结果且当前 turn 用户刚断言 `salary_floor_monthly=5000` → `ask_clarification` 反问 → 用户接受 → 二次检索；(c) 同 (b) 但用户拒绝 → `suggest_relaxation` 不放宽，且 `pending_relaxation` 已清；(d) 0 结果且 confidence 低 → `suggest_relaxation` 不放宽。 |

### 5.2.4 验收条件

1. **决策表单测覆盖**：每条规则 ≥ 2 条单测（典型 + 边界）；冲突优先级（如同时命中"低置信度"和"当前 turn slots_delta 触碰"）有显式断言。
2. **回归不退化**：阶段二 7+ 条 + 5.1 新增 2 条 golden 在 `post_search_policy_mode=on` 下全绿；其中"默认行为分支"的 golden 行为（auto_relax 三步级联）逐字节等价当前。**专门覆盖低召回 1/2 条命中**：构造 `initial_count=1` 和 `initial_count=2` 两条 golden，断言在 `low_recall_threshold=top_n=3` 下仍触发 reducer + auto_relax 默认分支（**不**因为不是 0 结果就跳过放宽，与当前 [search_service.py:193](../backend/app/services/search_service.py) `len(candidates) < top_n` 行为一致）。
3. **二阶段递归限制**：单测验证一次主搜索最多触发 1 次 `execute_relaxed_search` + 1 次 reducer 第二轮；防止 reducer 第二轮再输出 `auto_relax_and_retry` 导致死循环。
4. **流程隔离单测**：`pending_relaxation` 与 `pending_interruption` 同时非空时（极端情况），`_route_v2_resolve_conflict` 只读 `pending_interruption`、`_route_v2_relaxation_response` 只读 `pending_relaxation`，互不影响；单测显式构造该状态验证两条分发函数零交叉。**额外断言**：在 `_handle_text` 的 short-circuit 分支顺序里，`respond_relaxation_offer` 与 `resolve_conflict` 出现在同一层（都先于通用 dispatch），grep 确认两者不嵌套也不互相绕过。
5. **执行归属契约单测**：构造 `DialogueDecision(dialogue_act=respond_relaxation_offer, relaxation_response=accept, state_transition=apply_relaxation)` + `pending_relaxation` 非空，调 [dialogue_applier.apply_decision](../backend/app/services/dialogue_applier.py) 后断言 `session.pending_relaxation` **未**被清（apply_relaxation 不归 dialogue_applier 管）且日志为 `dialogue_applier_relaxation_passthrough` 而**非** unknown_transition warning；再调 `_route_v2_relaxation_response` 后断言 `pending_relaxation` 已清且返回的 `ReplyMessage` 列表含放宽后搜索结果文案。反向单测：state_transition=`clear_pending_relaxation` 时 `apply_decision` 直接清字段，不必走 `_route_v2_relaxation_response`。**调用边界单测**：直接 mock `apply_decision` 验证 `_handle_text` 的 short-circuit 分支在 `apply_relaxation` / `cancel_relaxation` 路径下**未**调用 `apply_decision`（mock.assert_not_called），仅在 `clear_pending_relaxation` 路径下被调一次。
6. **二次放宽护栏单测**：构造完整端到端场景：用户主搜索 `salary_floor_monthly=5000` 0 命中 → reducer 输出 `ask_clarification(step="relax_salary_10pct")` → 写 `pending_relaxation={original_criteria: {salary_floor_monthly: 5000}, relaxed_criteria: {salary_floor_monthly: 4500}, step: "relax_salary_10pct"}` → 用户回 "好的"。断言：(a) `_route_v2_relaxation_response` 调用 `execute_relaxed_search` 时**第一参数是 `{salary_floor_monthly: 5000}`**（original 非 relaxed）；(b) `execute_relaxed_search` 内部最终查 SQL 用的 `salary_floor_monthly=4500`（一次放宽）；(c) 不出现 `salary_floor_monthly=4050` 这种二次放宽值。**Grep 守护**：grep `_route_v2_relaxation_response` 函数体，**不出现**对 `pending_relaxation["relaxed_criteria"]` 的读取（只允许 `pending_relaxation["original_criteria"]`）。
7. **execute_relaxed_search 完整签名单测**：直接调 `execute_relaxed_search(original_criteria, step, direction=..., raw_query=..., session=..., user_ctx=..., db=..., user_msg_id=...)`，断言：(a) 内部确实调到了 reranker（mock `_rerank_with_logging` 验证 `user_msg_id / call_site` 字段被透传）；(b) 调到了 `save_snapshot / record_shown`（验证 session 写入）；(c) 调到了 `permission_service.filter_*`（验证 user_ctx 被消费）。少传任何一个 keyword-only 参数应抛 TypeError（Python 默认行为，不需要额外断言）。
8. **raw_query 来源单测（P1 #2 防退化）**：构造场景：用户主搜索原文 `"西安饭店服务员 5000"` 触发反问 → `pending_relaxation` 持久化 `raw_query="西安饭店服务员 5000"` → 下一轮用户回 `"好的"`（`msg.content="好的"`）。断言：(a) `_route_v2_relaxation_response` 调 `execute_relaxed_search` 时 `raw_query="西安饭店服务员 5000"`，**不是** `"好的"`；(b) reducer 第二轮构造的 `PostSearchContext.raw_query` 也是 `"西安饭店服务员 5000"`，与二次 reranker 一致。**Grep 守护**：grep `_route_v2_relaxation_response` 函数体，**不出现** `msg.content` 作为 `raw_query` 入参的字面量赋值（只允许 `pending["raw_query"]` 或等价读法）。
9. **TTL 与清理**：`pending_relaxation` 在以下场景必须被清空：用户接受/拒绝放宽后、用户开启新搜索（`reset_search`）、用户走 `cancel`、TTL 过期、search_active → idle 切换；每条路径单测覆盖。
10. **历史 dialogue_act 不被读取**：grep `post_search_reducer.py` 中所有 `parse_result.` / `session.` 读字段位置，**不出现**对历史 turn dialogue_act 的引用（防止开发时偷偷加幻象信号源）。
11. **关键词无新增**：grep [keyword-rules-audit.md](keyword-rules-audit.md) 中"放宽 / 调整 / 同意"等候选词，**未出现**任何新中文关键词列表（违反 §跨阶段共同约束 7）。
12. **shadow ≥ 1 周**：开发期 shadow 数据采集 ≥ 1 周（开发期上限可缩短到 3 天，但生产灰度仍需 1 周），分析三类指标：
   - `auto_relax_acceptance_rate`：用户对 `ask_clarification` 反问的接受率；
   - `zero_result_clarification_ratio`：0 结果场景中走 `ask_clarification` vs `auto_relax_and_retry` vs `suggest_relaxation` 的占比；
   - `recursion_overflow_rate`：reducer 第二轮越界率，必须为 0。

---

## 5.3 软偏好排序

定位：让 reranker 真正消费 [slot_schema.py](../backend/app/dialogue/slot_schema.py) 中 `filter_mode=soft` 的字段。这是 Phase 5 风险最高的一步，因为会影响所有搜索的 ranked_items 顺序。

### 5.3.1 功能

1. **`Reranker.rerank` 接口扩参（在 5.0 已声明，现在开始消费）**：
   ```python
   def rerank(
       self,
       query: str,
       candidates: list[dict],
       role: str,
       top_n: int = 3,
       *,
       soft_preferences: dict | None = None,   # 例如 {"provide_meal": True, "shift_pattern": "日班"}
       ranking_weights: dict[str, float] | None = None,  # 例如 {"provide_meal": 0.3, "shift_pattern": 0.2}
   ) -> RerankResult:
   ```

2. **prompt 升级**：`RERANK_PROMPT_VERSION` 升级到 `v2`，prompt 模板中加入：
   - 用户软偏好字段清单（结构化键值，不是自然语言）；
   - 每个偏好的权重（0~1）；
   - 明确指令"优先排序匹配软偏好的候选；硬过滤已保证基础匹配，软偏好仅影响顺序"；
   - 保留 v1 prompt 作为向后兼容（当 `soft_preferences=None` 时仍走 v1）。

3. **`slot_schema` 软偏好字段权重表**：把 `provide_meal / provide_housing / shift_pattern / dorm_condition / accept_couple / accept_student / accept_minority` 等 `filter_mode=soft` 字段的 `ranking_weight` 从 `None` 解锁为具体值。**初始权重不拍脑袋**：从历史搜索日志中分析"用户主动提及该偏好后接受第几个候选的占比"反推权重，写在 schema 文件顶部注释中，留迭代空间。

4. **构造 `soft_preferences` 入参**：search_service 调 reranker 前，从 `criteria` 中按 `slot_schema.fields_for(frame, filter_mode="soft")` 抽出所有软偏好字段（已在 criteria 中存在的）；从 `slot_schema` 拿对应 `ranking_weight`；构造 `soft_preferences / ranking_weights` 传入 reranker。

5. **`SearchOutcome.soft_pref_hits` 真实统计**：rerank 完成后，applier/search_service 统计 ranked_items 中各软偏好字段的命中数，写入 `SearchOutcome.soft_pref_hits`，**供 5.4 的 `show_results_with_soft_pref_notice` 文案使用**（5.1 不消费该字段，5.0 仅做 dataclass 字段位预留并写默认空 dict）。

### 5.3.2 边界

- **不**改 reranker 算法实现（如换模型、改打分函数）—— 仅扩 prompt 入参。
- **不**让软偏好影响硬过滤：候选集仍由 `_query_jobs / _query_resumes` 严格过滤；软偏好只决定"已合格候选间的顺序"。
- **不**对 `filter_mode=display` 字段（如 `job_title`）做排序加权 —— 这类字段仅展示，无业务排序意义。
- **不**做"个性化权重"：当前所有用户共享 schema 中的静态权重；个性化留 Phase 6+。
- **不**接管所有 `_VALID_JOB_KEYS / _VALID_RESUME_KEYS` 中的非硬过滤字段；只接管 schema 显式标 `filter_mode=soft` 的字段集。

### 5.3.3 改动范围

| 文件 | 改动点 |
|---|---|
| [backend/app/llm/base.py](../backend/app/llm/base.py) | `Reranker.rerank` 抽象方法签名实质化（去掉"接收但忽略"注释）；docstring 明确软偏好语义。 |
| [backend/app/llm/prompts.py](../backend/app/llm/prompts.py) | 新增 `RERANK_PROMPT_V2`；`RERANK_PROMPT_VERSION` 提升到 `v2`；保留 v1 作为 fallback。 |
| `backend/app/llm/providers/doubao.py` / `qwen.py` / mock provider | 实现 `soft_preferences / ranking_weights` 的 prompt 拼接；至少一个 provider 真实接通 v2 prompt。其它 provider 至少做到"传了软偏好不报错、且回退到等价 v1 行为"。 |
| [backend/app/dialogue/slot_schema.py](../backend/app/dialogue/slot_schema.py) | `provide_meal / provide_housing / shift_pattern / dorm_condition / accept_couple / accept_student / accept_minority` 等字段的 `ranking_weight` 从 `None` 改为具体值（建议初始 0.1~0.3 区间）；新增 helper `extract_soft_preferences(criteria, frame) -> tuple[dict, dict]`。 |
| [backend/app/services/search_service.py](../backend/app/services/search_service.py) | `search_jobs / search_workers` 调 `_rerank_with_logging` 时传入 `soft_preferences / ranking_weights`；rerank 完成后统计 `soft_pref_hits` 写入 `SearchOutcome`。 |
| [backend/app/config.py](../backend/app/config.py) | `DialoguePolicy` 新增 `soft_preference_ranking_enabled: bool = False`，默认关闭；分阶段灰度。 |
| `backend/tests/unit/test_search_service.py` / `test_rerank_integration.py` | 单测：软偏好关时 prompt 等价 v1；软偏好开时 prompt 正确包含字段+权重；`soft_pref_hits` 统计正确；空 criteria 软偏好不传 `soft_preferences`。 |
| `backend/tests/fixtures/dialogue_golden/` | 新增 2 条 golden：worker 找包住岗位 + 候选集中含包住和不含包住 → 包住岗位排序前置；broker 找日班工人 + 同上。两条 golden **不**断言精确排序顺序（reranker 结果不稳定），仅断言"包住命中候选在 top_n 中的占比 ≥ 阈值"。 |

### 5.3.4 验收条件

1. **`soft_preference_ranking_enabled=False` 时逐字节等价 5.2**：用 record/replay 50 条会话回放，所有 reply 完全相同。
2. **`soft_preference_ranking_enabled=True` 时回归不退化**：所有历史 golden（不依赖软偏好排序的）行为不变，含 5.0/5.1/5.2 引入的 case。
3. **软偏好命中提升可观测**：shadow ≥ 1 周后，对比 v1 / v2 两侧的 `top_n 中软偏好命中占比`，v2 必须显著高于 v1（具体阈值由产品与开发期 baseline 数据共同确定，不在本文档预先写死）。
4. **不影响硬过滤召回**：候选集（rerank 前）数量在 v1/v2 两侧完全相同（差异为 0）—— 这是软偏好只影响顺序的关键不变量。
5. **provider 兜底**：mock provider 不实现 v2 时调用方仍能正常拿到 v1 等价结果，无异常抛出。
6. **权重表来源可追溯**：`slot_schema` 顶部注释中说明每个 `ranking_weight` 的取值依据（哪份日志分析、什么样本期）；评审通过后再合入主分支。

---

## 5.4 可见性文案 + 灰度推全

定位：在 5.3 真正接通软偏好排序后，**首次启用** `show_results_with_soft_pref_notice` 这个 action 的 reducer 输出与 applier 渲染（5.0 仅预声明 Literal、5.1/5.2/5.3 都不输出该 action），并把 Phase 5 整体推到 100% 灰度。

### 5.4.1 功能

1. **首次接通 `show_results_with_soft_pref_notice`**：reducer 在 `SearchOutcome.soft_pref_hits` 命中阈值满足时输出该 action；applier 删除"未实现 action → fallback no_action"的防御分支，正式渲染文案前缀。
2. **可见性文案模板库**：在 [slot_schema.py](../backend/app/dialogue/slot_schema.py) 中定义每个软偏好字段的"已优先展示"模板；多偏好命中时拼接文案（"已优先展示符合「包吃住、日班」偏好的岗位"）。
3. **命中阈值**：仅当 `soft_pref_hits[field] / len(ranked_items) ≥ 0.5` 时才打可见性文案，避免"只命中 1 个就吹"。
4. **灰度阶梯**：
   - 第 1 周：`post_search_policy_mode=on` + `soft_preference_ranking_enabled=True` 灰度 5%（hash 桶）；
   - 第 2 周：25%；
   - 第 3 周：50%；
   - 第 4 周：100%。
   - 任一阶段关键指标回退 ≥ 5% 立即回滚到上一比例。
5. **关键指标采集**：在阶段四监控大盘上新增 Phase 5 专用面板：
   - `post_search_action_distribution`：各 action 占比；
   - `auto_relax_acceptance_rate`：反问被接受的比例；
   - `paginate_no_more_user_action`：用户在翻完后下一轮的行为（重新搜索 / 离开 / 调整条件）；
   - `soft_pref_top_n_hit_rate`：top_n 中软偏好命中占比；
   - `recursion_overflow_rate`：reducer 第二轮越界率（必须 0）。

### 5.4.2 边界

- **不**做 A/B 实验对比"加文案 vs 不加文案的转化率"—— 留运营层。
- **不**做"用户主动表达不喜欢某偏好"的反向降权 —— Phase 6+。
- **5.4 自身**不再新增 `SessionState` 字段：可见性文案完全在单 turn 内由 reducer + applier 决定，不跨 turn 持久化。Phase 5 全程**仅** 5.2 引入唯一一个新字段 `SessionState.pending_relaxation`（不复用 `pending_interruption / last_intent` 已有结构，理由见 §5.2.1 第 5 项）。

### 5.4.3 改动范围

| 文件 | 改动点 |
|---|---|
| [backend/app/dialogue/slot_schema.py](../backend/app/dialogue/slot_schema.py) | 新增 `soft_preference_visibility_template(fields_hit) -> str`，按命中字段集合产出可见性文案；仅当命中阈值满足时返回非空。 |
| `backend/app/services/post_search_applier.py` | `show_results_with_soft_pref_notice` 渲染调用上述 helper；阈值判断在此。 |
| [backend/app/config.py](../backend/app/config.py) | `DialoguePolicy` 新增 `phase5_rollout_percentage: int = 0`（hash 桶 0~100）；与 `post_search_policy_mode` 联合控制灰度（mode=on 且 hash 命中桶才生效）。 |
| 监控大盘配置 | 新增 Phase 5 面板 SQL / 看板配置（按运维仓库实际位置）。 |
| `backend/tests/unit/test_post_search_applier.py` | 单测：阈值边界（命中比例 0.49 / 0.50 / 0.51）；多偏好拼接文案；空命中不出文案。 |

### 5.4.4 验收条件

1. **100% 灰度后稳定 ≥ 2 周**，关键指标对照 5.0 基线无回退（empty_search_result_rate / fallback 率 / avg_turns_to_search / JSON 解析失败率）。
2. **回滚演练**：从 100% 切回 0%，5 分钟内灰度比例归零，session 无残留状态错乱（worker / broker / factory 三种角色各演练 1 次）。
3. **文档同步**：[dialogue-intent-extraction-current-state.md](dialogue-intent-extraction-current-state.md) §3 更新结果感知策略段落；[keyword-rules-audit.md](keyword-rules-audit.md) 标记软偏好排序已接管对应词表（如有）。
4. **归档验收记录**：`docs/dialogue-intent-extraction-phase5-acceptance.md`（新文件）记录五维度评估，与阶段三验收文档同构。

---

## 跨阶段共同约束（继承自 phased-plan §跨阶段共同约束）

以下原则贯穿 Phase 5 全部子阶段，违反任一视为不达标：

1. **LLM 不写 session、reducer 也不写 session**：所有 session 写入由 applier 按声明式 `state_transition / post_search_action` 执行。
2. **`post_search_action` 跨 turn 不持久化**：`PostSearchDecision` 每次 turn 内生成、turn 结束即丢弃。**唯一例外**：放宽确认这类需要跨 turn 等待用户响应的场景，由 5.2 引入的专用 `SessionState.pending_relaxation` 字段承载。**不**复用 `pending_interruption`（upload_conflict 专用）和 `last_intent`（观测字段）；**不**复用 `dialogue_act=resolve_conflict`（upload_conflict 上下文）—— 放宽流程使用平行的 `dialogue_act=respond_relaxation_offer` + `_route_v2_relaxation_response` 分发函数，与上传冲突流程零交叉。
3. **clarification / 降级文案不依赖 LLM**：所有文案由 `slot_schema` 模板 + `relaxation_directions` 等 helper 生成，避免易碎文案断言。
4. **关键词列表只减不增**：Phase 5 严禁通过新增中文关键词列表识别"放宽 / 同意 / 调整 / 算了"等开放语言，必须由阶段二 `dialogue_act` 及其对应的闭集响应字段承担——**搜索放宽场景**用 `dialogue_act=respond_relaxation_offer` + `relaxation_response: Literal["accept","reject"]`；**上传冲突场景**继续用 `dialogue_act=resolve_conflict` + `conflict_action`。两套字段互不复用，避免开发时把放宽确认硬塞回 `conflict_action`。
5. **golden case 长期保留**：每个子阶段引入的 golden 在后续不得删除，只能升级断言。Phase 5 全部子阶段完成后，golden 集合从阶段四的 7+ 条扩到 16+ 条。
6. **回滚优先于修补**：任何子阶段的灰度切流，关键指标回退 ≥ 5% 立即回滚而非热修；热修必须有对应的 golden 增量。
7. **每个子阶段都必须有独立 PR + 独立验收**：不允许把 5.0~5.4 合到一个大 PR 一起 review。
8. **二阶段递归深度硬限制**：`post_search_reduce` 第二轮之后不允许再次输出 `auto_relax_and_retry`，由代码 assert 守护。

---

## 失败模式落地（Phase 5 专属）

| 场景 | 期望行为 |
|---|---|
| `post_search_reduce` 抛异常 | 退回 `PostSearchDecision(action="no_action")`，main path 走旧链路；记录 `post_search_reducer_error` 日志事件 |
| `execute_relaxed_search` 失败 | applier 不重试，直接展示 `suggest_relaxation` + 原始错误归因日志 |
| Reranker v2 prompt 解析失败 | provider 自动回退 v1 prompt 重试一次；仍失败按现有 `LLMParseError` 分支处理（空结果回落） |
| `soft_preferences` 字段值非法（如非 schema 枚举值） | search_service 在构造入参时按 `slot_schema.validate_slots_delta` drop 非法字段 + 日志，不传给 reranker |
| reducer 输出 `auto_relax_and_retry` 但 `available_relax_steps` 为空 | applier 改走 `suggest_relaxation`，日志 `post_search_relax_unavailable` |
| `paginate_no_more` 但 `relaxation_directions` 为空（criteria 已极简） | applier 兜底使用现有静态文案"已经是所有匹配结果了。要不要换城市或工种重新搜索？" |
| 5.0 接通后 reranker 收到 `soft_preferences=None` | 必须严格走 v1 prompt 路径（fixture 录制对比） |
| `pending_relaxation` TTL 过期，用户当前轮回应"好的" | LLM 不会输出 `respond_relaxation_offer`（无上下文）；message_router 闭集兜底也不命中（前置条件 `pending_relaxation` 非空不满足）；按普通 chitchat / start_search 处理，不误执行二次检索 |
| LLM 在 `pending_relaxation=None` 时仍输出 `respond_relaxation_offer` | reducer 视为误判，降级为 `chitchat`，记录 `dialogue_v2_relaxation_no_context` 日志 |
| `pending_relaxation` 与 `pending_interruption` 同时非空（极端：用户在 upload_conflict 中触发了搜索 + 0 结果反问） | 两个字段独立维护、独立清理；`_route_v2_resolve_conflict` 仅读 `pending_interruption`，`_route_v2_relaxation_response` 仅读 `pending_relaxation`；不允许互相覆盖或合并 |

---

## 阶段依赖关系一览

```text
阶段一-四（已完成，主链路稳定）
   └── Phase 5.0（接口契约 + DTO 预埋，零行为变更）
          └── Phase 5.1（reducer 接通 + show_more 降级）
                 └── Phase 5.2（0/低召回策略升级）
                        └── Phase 5.3（软偏好排序）
                               └── Phase 5.4（可见性文案 + 100% 灰度）
                                      └── Phase 6+（招聘领域建模、个性化排序，独立规划）
```

- Phase 5.0 → 5.1：5.0 合入主分支后允许并行启动 5.1 / 5.2 / 5.3 的开发分支，但合并顺序必须严格按依赖图。
- Phase 5.1 → 5.2：5.1 的 `ask_clarification` 渲染必须先稳定，5.2 才能产出该 action。
- Phase 5.2 → 5.3：软偏好排序与 0/低召回策略解耦；但 5.2 的 shadow 数据要先收 ≥ 3 天，再开 5.3 灰度，避免两层不稳定耦合。
- Phase 5.3 → 5.4：软偏好排序 100% 接通后才打可见性文案，避免文案与实际排序行为不一致。

---

## 附录 A：与现有四阶段文档的关系

- **本文档不重复 §跨阶段共同约束、§长期边界**：直接继承 [dialogue-intent-extraction-phased-plan.md](dialogue-intent-extraction-phased-plan.md) 中的跨阶段约束。
- **本文档不重新定义 `DialogueDecision` / `DialogueParseResult` 等已有 DTO**：仅在 §5.0 列出本阶段需要的字段扩展。
- **未来 Phase 6+ 不在本文档范围**：包括完整招聘 ontology、个性化排序、对话规划队列、引用消解、多租户策略系统。

## 附录 B：本文档解决了 phased-plan §Phase 5 的哪些遗留问题

phased-plan §Phase 5 列出 5 项功能，本文档对应安排如下：

| phased-plan §Phase 5 项 | 本文档子阶段 |
|---|---|
| 二阶段裁决 `post_search_reducer` | 5.0 接口 + 5.1 接通 |
| 0/低召回策略 | 5.2 |
| `show_more` 降级语义 | 5.1 |
| 软偏好排序 | 5.3 |
| 可见性文案 | 5.0 Literal 预声明 + 5.4 首次接通（5.1/5.2/5.3 不输出该 action） |
