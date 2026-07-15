"""搜索层 DTO 中立模块（Phase 5 §5.0）。

把原本散在 `app/services/search_service.py` 里的 `SearchResult /
FallbackSuggestion / FallbackOutcome` 三个 dataclass 集中到这里，加上 Phase 5
新增的 `SearchOutcome`。

模块仅依赖 stdlib + typing，**禁止** import 任何 `app.services.*`，避免
`search_service` 与 `post_search_reducer` 之间形成循环 import。

phased-plan §5.0.1 第 4 项给出的规则：
- `search_service.py` 改为从本模块 import + re-export，调用方 `search_service.SearchResult`
  仍可工作（向后兼容现有测试）。
- `post_search_reducer.py` 也只 import 本模块，不 import `search_service`。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


# ---------------------------------------------------------------------------
# 搜索回复 DTO（迁移自 search_service.py）
# ---------------------------------------------------------------------------

@dataclass
class SearchResult:
    """搜索（含 show_more）的最终回复结果。

    迁移说明：本类原定义在 `app/services/search_service.py`，Phase 5 §5.0 为
    解循环 import 迁来本模块。字段语义保持不变。
    """
    reply_text: str
    has_more: bool = False
    result_count: int = 0


@dataclass(frozen=True)
class MatchReason:
    """A deterministic, safe match explanation rendered only when gated on."""

    kind: Literal["hard_match", "soft_preference"]
    text: str
    field: str = ""


@dataclass(frozen=True)
class RelaxationSummary:
    """Visible-count summary for auto/confirmed relaxation copy."""

    field: str
    label: str
    original_criteria: dict
    relaxed_criteria: dict
    original_visible_count: int = 0
    relaxed_visible_count: int = 0
    relaxed_shown_count: int = 0


@dataclass(frozen=True)
class FallbackSuggestion:
    """激进放宽探查命中的方向（Bug 3，迁移自 search_service.py）。

    仅用于文案提示，不会自动用这个 criteria 返回结果——避免把不符原意的
    岗位/简历当作"找到的"展示给用户。
    """
    step: str           # _SUGGESTION_LABEL_* 中的 key
    criteria: dict      # 探查时使用的 criteria（拷贝，外部不要 mutate）
    count: int          # 该 criteria 下的候选数（≥1 才会进 suggestions）


@dataclass
class FallbackOutcome:
    """fallback 步骤的结构化产物（Bug 3，迁移自 search_service.py）。

    - candidates：最终选用的候选列表（沿用既有"严格更优才采纳"语义）
    - applied_step：哪一步被采纳；None 表示用原 criteria 命中或全部 0 召回
    - suggestions：当 candidates 为空时探查到的激进放宽方向（已确认 ≥1）
    """
    candidates: list
    applied_step: str | None = None
    suggestions: list[FallbackSuggestion] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Phase 5 新增：SearchOutcome
# ---------------------------------------------------------------------------

@dataclass
class SearchOutcome:
    """搜索过程信息 DTO（Phase 5 §5.0.1 第 3 项）。

    与 `SearchResult` 并存：`SearchResult` 表达"给用户的回复"，本类表达"reducer
    决策需要的过程信息"。`search_jobs / search_workers / show_more` 在 5.0 起
    都返回 `tuple[SearchResult, SearchOutcome]`。

    Phase 5.0 仅产出本结构、不消费；5.1 之后由 `post_search_reduce` 读取决策。

    字段语义见 phased-plan §5.0.1 第 3 项 + §5.0.1 SearchOutcome 注释。
    """
    direction: Literal["search_job", "search_worker"]
    """检索方向，决定走 _query_jobs 还是 _query_resumes。"""

    criteria_used: dict
    """实际跑 SQL 的 criteria（含放宽后版本）。

    5.2 regime 下：主搜索时 = 用户原始 criteria（未放宽，因为 5.2 把放宽决策
    移交给 reducer）；二次检索（execute_relaxed_search 返回的 outcome）时 =
    放宽后的 criteria。
    """

    initial_count: int
    """原 criteria 下的硬过滤命中数（放宽前）。"""

    final_count: int
    """放宽 / fallback 后实际返回的候选数。"""

    desired_count: int
    """本次搜索期望返回的候选数（= top_n，由 search_service 注入）。"""

    low_recall_threshold: int
    """触发 fallback 的阈值；当前 search_service 用 top_n 本身作为阈值
    （即 initial_count < top_n 即视为低召回）。
    5.2 reducer 复用此阈值保持等价。"""

    candidate_count_capped: int | None = None
    """SQL/内存候选数，可能受 max_candidates 截断，不直接宣称为全库总数。"""

    visible_count: int | None = None
    """权限过滤后可展示数量，用户可见文案优先使用该字段。"""

    shown_count: int | None = None
    """本次实际进入 reply 的数量。"""

    probe_count: int | None = None
    """放宽探测数量，只能作为约数或方向可用信号。"""

    remaining_count_capped: int | None = None
    """基于当前 snapshot 与已展示 ID 计算的剩余可展示数量。"""

    applied_relax_step: str | None = None
    """已采纳的放宽步（None=未放宽）。"""

    fallback_suggestions: list[FallbackSuggestion] = field(default_factory=list)
    """0 召回探查到的方向（与 FallbackOutcome.suggestions 同源）。"""

    soft_pref_hits: dict[str, int] = field(default_factory=dict)
    """候选集中各软偏好字段命中数（供 5.4 可见性文案使用；
    5.0/5.1/5.2/5.3 不消费，仅写入空 dict 占位）。"""

    has_more: bool = False
    """是否还有未展示候选（用于 show_more 的 has_more 派生）。"""

    snapshot_exhausted: bool = False
    """show_more 调用且快照已翻完（5.1 paginate_no_more 决策依据）。"""

    available_relax_steps: list[str] = field(default_factory=list)
    """search_service 探查后告诉 reducer 可走的放宽步名（5.2 使用）。"""

    relax_probe_results: list[dict] = field(default_factory=list)
    """探查每步的候选数（5.2 使用，含 step / count）。"""

    relaxation_summary: RelaxationSummary | None = None
    """自动/确认放宽后的用户可见摘要，保留原条件与放宽后数量。"""
