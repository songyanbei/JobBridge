"""检索服务（Phase 3）。

三步漏斗：硬过滤 → Reranker 重排 → 权限过滤 → 文本格式化。
show_more 复用快照，不重新执行全量检索。

Phase 7：在 LLM 调用处补 loguru 结构化打点（llm_call 事件）。
"""
import json
import hashlib
import logging
import math
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.config import settings
from app.core.exceptions import LLMError, LLMParseError, LLMTimeout
from app.core.time_utils import rotation_date, utc_now
from app.llm import get_reranker
from app.llm.base import RerankResult
from app.models import Job, Resume, SystemConfig, User
from app.schemas.conversation import SessionState
# Phase 5 §5.0：DTO 中立模块迁移。本模块仍可作为 search_service.SearchResult 等旧名
# 的访问入口（re-export），避免破坏 backend/tests/unit/test_search_service.py 等
# 直接 from app.services.search_service import SearchResult 的调用方。
from app.schemas.search import (
    FallbackOutcome,
    FallbackSuggestion,
    RelaxationSummary,
    SearchOutcome,
    SearchResult,
)
from app.schemas.recommendation import RecommendationItem, StrategyAssignment
from app.services import conversation_service, permission_service
from app.services.recommendation_experience_gate import RecommendationExperienceFlags
from app.services.recommendation_experience_gate import userid_hash
from app.services.recommendation_reason_service import (
    build_match_reasons,
    project_job_for_explanation,
    project_resume_for_explanation,
    render_match_reasons,
)
from app.services.recommendation_scoring_service import (
    V1_DISPLAY_TOP_N,
    V1_MAX_CANDIDATES,
    V1_PRECISION_POOL_SIZE,
)
from app.services.user_service import UserContext
from app.tasks.common import log_event

# 显式 re-export，让 mypy / IDE / runtime 都识别本模块仍提供这些名字。
__all__ = [
    "FallbackOutcome",
    "FallbackSuggestion",
    "SearchOutcome",
    "SearchResult",
    "search_jobs",
    "search_workers",
    "show_more",
    "has_effective_search_criteria",
]

logger = logging.getLogger(__name__)

# §11.5: this used to be a local "v1" literal that shadowed the real prompt
# version, so every llm_call log event reported a version the prompt had not
# used since v2.  Re-export the single source of truth instead.
from app.llm.prompts import RERANK_PROMPT_VERSION  # noqa: E402
_queue_backlog_hint: ContextVar[int] = ContextVar("queue_backlog_hint", default=0)


def _query_attempt_record(
    *,
    step: str,
    criteria: dict,
    candidates: list,
    started: float,
) -> dict:
    """Build the durable facts for one real SQL candidate query (§9.5)."""
    criteria_body = json.dumps(
        criteria or {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    candidate_ids = [
        str(
            item.get("id") if isinstance(item, dict)
            else getattr(item, "id", "")
        )
        for item in candidates
    ]
    candidate_ids = [candidate_id for candidate_id in candidate_ids if candidate_id]
    return {
        "step": step,
        "attempt_kind": "initial" if step == "initial" else "relax_probe",
        "criteria_digest": hashlib.sha256(
            criteria_body.encode("utf-8"),
        ).hexdigest(),
        "candidate_count": len(candidate_ids),
        "candidate_ids": candidate_ids[:V1_MAX_CANDIDATES],
        "precision_pool_ids": [],
        "result_count": len(candidate_ids),
        "is_zero_result": not candidate_ids,
        "scoring_time_utc": utc_now(),
        "llm_status": "skipped",
        "llm_retry_count": 0,
        "ranking_latency_ms": max(0, int((time.perf_counter() - started) * 1000)),
    }


def set_queue_backlog_hint(depth: int):
    return _queue_backlog_hint.set(max(0, int(depth)))


def reset_queue_backlog_hint(token) -> None:
    _queue_backlog_hint.reset(token)


def _json_scalar(value: object) -> str:
    """Serialize a value as a JSON string scalar for MySQL JSON_CONTAINS."""
    return json.dumps(str(value), ensure_ascii=False)


def _job_salary_covers_floor(salary_floor: int):
    return sa.or_(
        Job.salary_ceiling_monthly >= salary_floor,
        sa.and_(
            Job.salary_ceiling_monthly.is_(None),
            Job.salary_floor_monthly >= salary_floor,
        ),
    )


def _is_phase5_policy_enabled_for_user(userid: str | None) -> bool:
    from app.services.intent_service import is_phase5_policy_enabled

    return is_phase5_policy_enabled(userid or "")


def _normalize_experience_flags(
    experience_flags: RecommendationExperienceFlags | None,
) -> RecommendationExperienceFlags:
    return experience_flags or RecommendationExperienceFlags.disabled()


# Stage B：0 命中 fallback 文案（§3.5）。
# 不能伪装成推荐结果；必须明确告知未找到并给出可操作建议。
NO_JOB_MATCH_REPLY = (
    "暂未找到符合条件的岗位。可以放宽城市、工种或薪资范围后再试，"
    "也可以补充更多偏好（例如包吃住、班次）让我重新筛选。"
)
NO_WORKER_MATCH_REPLY = (
    "暂未找到匹配的求职者。可以放宽城市或工种条件，"
    "或补充期望年龄、经验等限制让我重新筛选。"
)

# Bug 3：fallback 采纳某步后，给搜索结果加的前缀提示。
# 这样用户能看到"系统自动放宽了什么"，而不是把结果当成原条件命中。
_FALLBACK_NOTICE_JOB = {
    "relax_salary_10pct": "原条件无匹配，已自动把薪资下限放宽 10% 后重新搜索。",
    "broaden_job_category": "原条件无匹配，已自动把工种放宽到大类后重新搜索。",
    "drop_optional_filters": "原条件无匹配，已自动去掉部分次要条件后重新搜索。",
}
_FALLBACK_NOTICE_RESUME = {
    "relax_salary_10pct": "原条件无匹配，已自动把期望薪资上限放宽 10% 后重新搜索。",
    "broaden_job_category": "原条件无匹配，已自动把工种放宽到大类后重新搜索。",
    "drop_optional_filters": "原条件无匹配，已自动去掉部分次要条件后重新搜索。",
}

# Bug 3：所有温和放宽都 0 召回时，再做激进探查给用户做"建议方向"。
# 探查步只产出 suggestions（不采纳为结果），所以即使去掉薪资/工种也不会
# 让用户在不知情下看到不符合原意的岗位。
_SUGGESTION_LABEL_JOB = {
    "drop_salary": "不限薪资",
    "drop_job_category": "不限工种",
    "keep_city_only": "只保留城市",
}
_SUGGESTION_LABEL_RESUME = {
    "drop_salary_ceiling": "不限期望薪资",
    "drop_job_category": "不限工种",
    "keep_city_only": "只保留城市",
}
_MAX_SUGGESTIONS = 3


def _relax_step_label(direction: str, step: str) -> str:
    job_labels = {
        "relax_salary_10pct": "放宽薪资下限",
        "broaden_job_category": "放宽工种到大类",
        "drop_optional_filters": "去掉部分次要条件",
    }
    resume_labels = {
        "relax_salary_10pct": "放宽期望薪资上限",
        "broaden_job_category": "放宽工种到大类",
        "drop_optional_filters": "去掉部分次要条件",
    }
    labels = job_labels if direction == "search_job" else resume_labels
    return labels.get(step, "放宽部分条件")


def _format_relaxation_value(value: object) -> str:
    if value is None:
        return "不限"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (list, tuple, set)):
        return "、".join(str(item) for item in value)
    return str(value)


def _relaxation_value_change(summary: RelaxationSummary) -> str:
    """Render only safe, user-facing criteria whose value actually changed."""
    if summary.field == "relax_salary_10pct":
        keys = (
            ("salary_floor_monthly", "薪资下限"),
            ("salary_ceiling_monthly", "期望薪资上限"),
        )
    elif summary.field == "broaden_job_category":
        keys = (("job_category", "工种"),)
    elif summary.field == "drop_optional_filters":
        keys = (
            ("salary_floor_monthly", "薪资下限"),
            ("salary_ceiling_monthly", "期望薪资上限"),
            ("job_category", "工种"),
            ("gender_required", "性别"),
            ("gender", "性别"),
            ("is_long_term", "长期岗位"),
            ("age", "年龄"),
        )
    else:
        keys = ()

    changes: list[str] = []
    for key, label in keys:
        original_present = key in summary.original_criteria
        relaxed_present = key in summary.relaxed_criteria
        original = summary.original_criteria.get(key)
        relaxed = summary.relaxed_criteria.get(key)
        if original_present == relaxed_present and original == relaxed:
            continue
        if not original_present:
            original = None
        if not relaxed_present:
            relaxed = None
        original_text = _format_relaxation_value(original)
        relaxed_text = _format_relaxation_value(relaxed)
        if original_text == relaxed_text:
            continue
        changes.append(
            f"{label}{original_text} → {relaxed_text}"
        )
    return "；".join(changes)


def _render_relaxation_summary_notice(summary: RelaxationSummary) -> str:
    original = summary.original_visible_count
    relaxed = summary.relaxed_visible_count
    shown = summary.relaxed_shown_count
    value_change = _relaxation_value_change(summary)
    label = f"{summary.label}（{value_change}）" if value_change else summary.label
    parts = [
        f"原条件下可展示 {original} 条结果，我已为你{label}后重新搜索。",
        f"放宽后本次展示 {shown} 条",
    ]
    if relaxed != shown:
        parts[-1] += f"，当前可见 {relaxed} 条"
    parts[-1] += "。"
    return "\n".join(parts)


def _build_job_reason_lines_by_id(
    jobs: list[dict],
    criteria: dict,
    flags: RecommendationExperienceFlags,
    soft_preferences: dict | None = None,
    external_userid_hash: str = "",
) -> dict[str, list[str]]:
    return _build_reason_lines_by_id(
        jobs,
        criteria,
        flags,
        item_type="job",
        soft_preferences=soft_preferences,
        external_userid_hash=external_userid_hash,
    )


def _build_resume_reason_lines_by_id(
    resumes: list[dict],
    criteria: dict,
    flags: RecommendationExperienceFlags,
    soft_preferences: dict | None = None,
    external_userid_hash: str = "",
) -> dict[str, list[str]]:
    return _build_reason_lines_by_id(
        resumes,
        criteria,
        flags,
        item_type="resume",
        soft_preferences=soft_preferences,
        external_userid_hash=external_userid_hash,
    )


def _build_reason_lines_by_id(
    items: list[dict],
    criteria: dict,
    flags: RecommendationExperienceFlags,
    *,
    item_type: Literal["job", "resume"],
    soft_preferences: dict | None = None,
    external_userid_hash: str = "",
) -> dict[str, list[str]]:
    can_show_reasons = flags.show_match_reasons or flags.soft_preference_reasons
    if not (can_show_reasons or flags.build_shadow_reasons):
        return {}

    reason_lines: dict[str, list[str]] = {}
    reason_kinds: set[str] = set()
    for item in items:
        projected = (
            project_job_for_explanation(item)
            if item_type == "job"
            else project_resume_for_explanation(item)
        )
        reasons = build_match_reasons(
            item=projected,
            criteria=criteria,
            item_type=item_type,
            soft_pref_hits=_item_soft_preference_hits(item, soft_preferences),
            include_soft_preferences=flags.soft_preference_reasons,
        )
        if reasons:
            reason_lines[str(item.get("id", ""))] = render_match_reasons(reasons)
            reason_kinds.update(r.kind for r in reasons)

    if reason_lines:
        log_event(
            "match_explanation_built",
            external_userid_hash=external_userid_hash,
            direction="search_job" if item_type == "job" else "search_worker",
            item_type=item_type,
            explanation_count=sum(len(v) for v in reason_lines.values()),
            reason_kinds=sorted(reason_kinds),
            shadow_only=not can_show_reasons,
        )
    return reason_lines if can_show_reasons else {}


def _item_soft_preference_hits(
    item: dict,
    soft_preferences: dict | None,
) -> dict[str, bool]:
    if not soft_preferences:
        return {}
    hits: dict[str, bool] = {}
    for key, expected in soft_preferences.items():
        if item.get(key) == expected:
            hits[key] = True
    return hits


def _extract_soft_prefs_for_rerank(
    criteria: dict,
    frame: str,
    experience_flags: RecommendationExperienceFlags | None = None,
) -> tuple[dict, dict[str, float]]:
    """Phase 5 §5.3：受 settings.soft_preference_ranking_enabled 控制。
    关闭时返回空 dict，等价 5.0/5.1/5.2 路径（不传 soft_preferences 给 reranker）。
    开启时调 slot_schema.extract_soft_preferences。
    """
    flags = _normalize_experience_flags(experience_flags)
    if not flags.soft_preference_ranking:
        return {}, {}
    from app.dialogue import slot_schema
    return slot_schema.extract_soft_preferences(criteria, frame=frame)


def _count_soft_pref_hits(
    candidates: list[dict],
    soft_preferences: dict | None,
) -> dict[str, int]:
    """Phase 5 §5.4：统计候选集中各软偏好字段的命中数。

    candidate dict 中某字段值与 soft_preferences[key] 相等时算命中（bool 比较 + 字符串相等）。
    仅 soft_preference_ranking_enabled=True 时由调用方使用；False 时返回空 dict。
    """
    if not soft_preferences or not candidates:
        return {}
    hits: dict[str, int] = {}
    for key, expected in soft_preferences.items():
        count = sum(1 for c in candidates if c.get(key) == expected)
        if count > 0:
            hits[key] = count
    return hits


def _rerank_with_logging(
    query: str,
    candidates: list[dict],
    role: str,
    top_n: int,
    call_site: str,
    user_msg_id: str | None = None,
    soft_preferences: dict | None = None,
    ranking_weights: dict[str, float] | None = None,
) -> RerankResult:
    """统一封装 reranker.rerank，附带 loguru 结构化打点。

    Phase 7：``llm_call`` 日志含 input_tokens / output_tokens / user_msg_id，
    便于成本分析、定位单条消息对应的检索链路。任何 reranker 故障都回落为空
    ``ranked_items``；调用方会把 SQL 候选按稳定查询顺序补回，保证排序服务故障
    不会让已有业务候选不可用。
    """
    start = time.perf_counter()
    status = "ok"
    result: RerankResult | None = None
    try:
        threshold = max(0, int(settings.reranker_queue_degrade_threshold or 0))
        queue_depth = _queue_backlog_hint.get()
        if threshold and queue_depth >= threshold:
            status = "backlog_degraded"
            result = RerankResult(
                ranked_items=[], reply_text="", raw_response="",
            )
            log_event(
                "reranker_backlog_degraded",
                queue_depth=queue_depth,
                threshold=threshold,
                call_site=call_site,
            )
            return result

        reranker = get_reranker()
        # Phase 5 §5.3：soft_preferences 非空时透传给 reranker（v2.1 prompt）；
        # 为空时严格走 v2.0 路径。两条路径都通过 keyword-only 参数。
        rerank_kwargs = {
            "query": query,
            "candidates": candidates,
            "role": role,
            "top_n": top_n,
        }
        if soft_preferences:
            rerank_kwargs["soft_preferences"] = soft_preferences
            rerank_kwargs["ranking_weights"] = ranking_weights
        result = reranker.rerank(**rerank_kwargs)
    except LLMTimeout as exc:
        status = "timeout"
        logger.warning("reranker timeout at %s; using deterministic fallback", call_site)
        result = RerankResult(
            ranked_items=[], reply_text="", raw_response="",
            input_tokens=getattr(exc, "input_tokens", None),
            output_tokens=getattr(exc, "output_tokens", None),
            retry_count=int(getattr(exc, "llm_retry_count", 0) or 0),
        )
    except LLMParseError as exc:
        # 空结果回落，后续业务按 0 召回处理；不再 raise 以对齐 intent 侧策略。
        # provider 在 raise 前已把 token 挂到 exc.input_tokens / exc.output_tokens，
        # 这里回读到 fallback RerankResult，保证 log_event 记录真实 token 用量。
        status = "parse_failed"
        result = RerankResult(
            ranked_items=[],
            reply_text="",
            raw_response="",
            input_tokens=getattr(exc, "input_tokens", None),
            output_tokens=getattr(exc, "output_tokens", None),
            retry_count=int(getattr(exc, "llm_retry_count", 0) or 0),
        )
    except LLMError as exc:
        status = "http_error"
        logger.warning("reranker HTTP error at %s; using deterministic fallback", call_site)
        result = RerankResult(
            ranked_items=[], reply_text="", raw_response="",
            input_tokens=getattr(exc, "input_tokens", None),
            output_tokens=getattr(exc, "output_tokens", None),
            retry_count=int(getattr(exc, "llm_retry_count", 0) or 0),
        )
    except Exception as exc:
        # 非 LLMError 家族的意外异常（如 provider 实现 bug、类型错误等）。
        # 可用性优先：同样退回 SQL 顺序，但保留 exception stack 供告警归因。
        status = "unknown_error"
        logger.exception(
            "reranker unexpected error at %s; using deterministic fallback", call_site,
        )
        result = RerankResult(
            ranked_items=[], reply_text="", raw_response="",
            input_tokens=getattr(exc, "input_tokens", None),
            output_tokens=getattr(exc, "output_tokens", None),
            retry_count=int(getattr(exc, "llm_retry_count", 0) or 0),
        )
    finally:
        duration_ms = int((time.perf_counter() - start) * 1000)
        if result is not None:
            result.llm_status = (
                status if status in {"ok", "timeout", "http_error", "parse_failed"}
                else "skipped" if status == "backlog_degraded"
                else "http_error"
            )
            result.latency_ms = max(0, duration_ms)
            result.ranking_fallback = None if status == "ok" else status[:32]
        log_event(
            "llm_call",
            call_site=call_site,
            provider=settings.llm_provider,
            model=settings.llm_reranker_model,
            prompt_version=RERANK_PROMPT_VERSION,
            duration_ms=duration_ms,
            candidate_count=len(candidates),
            top_n=top_n,
            ranked_count=len(result.ranked_items) if result else 0,
            input_tokens=getattr(result, "input_tokens", None),
            output_tokens=getattr(result, "output_tokens", None),
            user_msg_id=user_msg_id,
            status=status,
        )
    return result


# Phase 5 §5.0：SearchResult / FallbackSuggestion / FallbackOutcome 已迁到
# app/schemas/search.py，本模块仅 import + re-export，调用方不感知。


# ---------------------------------------------------------------------------
# SearchOutcome 构造 helper（Phase 5 §5.0）
# ---------------------------------------------------------------------------

def _build_search_outcome(
    *,
    direction: str,
    criteria_used: dict,
    initial_count: int,
    final_count: int,
    desired_count: int,
    applied_relax_step: str | None = None,
    fallback_suggestions: list[FallbackSuggestion] | None = None,
    has_more: bool = False,
    snapshot_exhausted: bool = False,
    soft_pref_hits: dict[str, int] | None = None,
    available_relax_steps: list[str] | None = None,
    relax_probe_results: list[dict] | None = None,
    candidate_count_capped: int | None = None,
    visible_count: int | None = None,
    shown_count: int | None = None,
    probe_count: int | None = None,
    remaining_count_capped: int | None = None,
    relaxation_summary=None,
) -> SearchOutcome:
    """构造 SearchOutcome；low_recall_threshold 默认等于 desired_count（top_n）。

    5.0 子阶段产出该结构但 reducer 默认 no_action，本字段不会被实际消费；
    5.1 起 reducer 才开始读取（见 phased-plan §5.0.1 第 3 项 / §5.2.1）；
    5.2 起 available_relax_steps / relax_probe_results 由 search_jobs/workers 在
    post_search_policy_mode=on 时填充（让 reducer 决定走哪个 relax 步骤）；
    5.4 起 soft_pref_hits 由 search_jobs/workers/execute_relaxed_search 真填充。
    """
    return SearchOutcome(
        direction=direction,
        criteria_used=dict(criteria_used or {}),
        initial_count=initial_count,
        final_count=final_count,
        desired_count=desired_count,
        low_recall_threshold=desired_count,
        candidate_count_capped=candidate_count_capped,
        visible_count=visible_count,
        shown_count=shown_count,
        probe_count=probe_count,
        remaining_count_capped=remaining_count_capped,
        applied_relax_step=applied_relax_step,
        fallback_suggestions=list(fallback_suggestions or []),
        soft_pref_hits=dict(soft_pref_hits or {}),
        has_more=has_more,
        snapshot_exhausted=snapshot_exhausted,
        available_relax_steps=list(available_relax_steps or []),
        relax_probe_results=list(relax_probe_results or []),
        relaxation_summary=relaxation_summary,
    )


def _legacy_fallback_assignment(decision) -> StrategyAssignment | None:
    """The assignment to record when v1 declines or fails.

    Dropping it to ``None`` makes the request fact read `execution_mode=off`,
    which is indistinguishable from an operator having switched v1 off. During a
    rollout the legacy share is the only signal that v1 is silently failing, so
    the release's real execution_mode has to survive the fallback — only the
    strategy identity degrades to legacy.
    """
    assignment = getattr(decision, "assignment", None)
    if assignment is None:
        return None
    return assignment.model_copy(update={
        "assignment": "legacy",
        "strategy_version_id": None,
        "algorithm_version": "legacy",
    })


def _served_recommendation_items(
    served_candidates: list[dict],
    direction: str,
    ranked_items: list[RecommendationItem] | None = None,
) -> list[RecommendationItem]:
    """Describe exactly the candidates rendered in the reply.

    Delivery/impression facts are required in off/legacy mode too.  They cannot
    reuse the pre-permission v1 list because doing so would count candidates
    that were filtered out and never appeared in the user-visible message.
    """
    ranked_by_id = {
        str(item.target_id): item for item in (ranked_items or [])
    }
    result: list[RecommendationItem] = []
    for position, candidate in enumerate(served_candidates, 1):
        candidate_id = int(candidate["id"])
        ranked = ranked_by_id.get(str(candidate_id))
        if ranked is not None:
            result.append(ranked.model_copy(update={
                "position": position,
                "owner_userid": candidate.get("owner_userid"),
            }))
            continue
        result.append(RecommendationItem(
            target_type="job" if direction == "search_job" else "resume",
            target_id=candidate_id,
            position=position,
            owner_userid=candidate.get("owner_userid"),
            final_score=0.0,
            is_exploration=False,
            reason_codes=["legacy_baseline"],
            score_detail=None,
        ))
    return result


def _search_attempt_fields(
    v1: dict | None,
    rerank_result: RerankResult,
    *,
    scoring_time_utc: datetime | None = None,
) -> dict:
    """Project serving-rank telemetry into the durable request/attempt DTO."""
    source = v1 or {}
    return {
        "scoring_time_utc": (
            source.get("scoring_time_utc") or scoring_time_utc or utc_now()
        ),
        "llm_status": source.get("llm_status", rerank_result.llm_status),
        "llm_input_tokens": source.get(
            "llm_input_tokens", rerank_result.input_tokens,
        ),
        "llm_output_tokens": source.get(
            "llm_output_tokens", rerank_result.output_tokens,
        ),
        "llm_retry_count": int(source.get(
            "llm_retry_count", rerank_result.retry_count,
        ) or 0),
        "ranking_fallback": source.get(
            "ranking_fallback", rerank_result.ranking_fallback,
        ),
        "ranking_latency_ms": int(source.get(
            "ranking_latency_ms", rerank_result.latency_ms,
        ) or 0),
    }


def _snapshot_is_v1(snapshot) -> bool:
    """A snapshot produced by recommendation-v1 rather than the legacy ranker."""
    if snapshot is None:
        return False
    return (
        getattr(snapshot, "algorithm_version", "legacy") != "legacy"
        or getattr(snapshot, "assignment", "legacy") != "legacy"
    )


def _recommendation_kill_switch(db: Session) -> bool:
    """§7.5 fail-safe: an unreadable control plane counts as killed."""
    try:
        from app.services.recommendation_strategy_service import runtime_kill_switch

        return bool(runtime_kill_switch(db))
    except Exception:
        logger.warning("runtime control unreadable; treating as kill switch on", exc_info=True)
        return True


def _try_recommendation_v1(
    *,
    candidate_dicts: list[dict],
    direction: str,
    criteria: dict,
    userid: str,
    db: Session,
    raw_query: str,
    assignment_decision=None,
    request_now_utc: datetime | None = None,
):
    """Run v1 only when its DB release is enabled; any control-plane failure
    deliberately falls back to the existing legacy caller."""
    try:
        from app.services.recommendation_assignment_service import choose_assignment
        from app.services.recommendation_request_service import precision_pool, rank_candidate_dicts
        assignment = assignment_decision or choose_assignment(
            db, userid=userid, direction=direction,
        )
        if assignment.assignment.assignment == "legacy" or not assignment.version:
            return None
        query_digest = conversation_service.compute_query_digest(criteria)
        precision_ids = precision_pool(
            candidate_dicts,
            direction=direction,
            criteria=criteria,
            userid=userid,
            query_digest=query_digest,
        )
        candidates_by_id = {
            str(item.get("id")): item for item in candidate_dicts
        }
        precision_candidates = [
            candidates_by_id[candidate_id]
            for candidate_id in precision_ids
            if candidate_id in candidates_by_id
        ]
        request_now_utc = request_now_utc or utc_now()
        semantic_result = _rerank_with_logging(
            query=raw_query,
            candidates=precision_candidates,
            role="worker" if direction == "search_job" else "factory",
            top_n=V1_DISPLAY_TOP_N,
            call_site=f"recommendation_v1_{direction}",
        )
        candidate_ids = [str(item.get("id")) for item in candidate_dicts[:V1_MAX_CANDIDATES]]
        target_type = "job" if direction == "search_job" else "resume"
        parameters = assignment.version.parameters
        # §10.6: exposure reads are an observability side channel.  A failure
        # degrades the exposure component to the neutral 0.5 for every candidate
        # and keeps the request on v1 — it must not drag the whole strategy back
        # to legacy the way a control-plane failure does.
        from app.services.recommendation_exposure_service import (
            batch_candidate_exposures,
            recent_user_exposures,
        )
        try:
            exposure_counts = batch_candidate_exposures(
                db, target_type=target_type, candidate_ids=candidate_ids,
                request_now_utc=request_now_utc,
            )
            recent_exposures = recent_user_exposures(
                db, viewer_userid=userid, target_type=target_type,
                candidate_ids=candidate_ids, request_now_utc=request_now_utc,
                cooldown_hours=int(parameters.get("repeat_cooldown_hours", 24)),
            )
            exposure_available = True
        except Exception:
            logger.warning(
                "recommendation-v1 exposure read failed; falling back to neutral "
                "exposure opportunity", exc_info=True,
            )
            exposure_counts, recent_exposures, exposure_available = {}, {}, False
        ordered, items = rank_candidate_dicts(
            candidate_dicts,
            direction=direction,
            criteria=criteria,
            userid=userid,
            query_digest=query_digest,
            strategy_version=assignment.version.id,
            parameters=parameters,
            semantic_ranked_items=semantic_result.ranked_items,
            exposure_counts=exposure_counts,
            recent_exposures=recent_exposures,
            exposure_available=exposure_available,
            precision_pool_ids=precision_ids,
            rotation_date=rotation_date(request_now_utc),
            now=request_now_utc,
        )
        return {
            "ids": [candidate.candidate_id for candidate in ordered],
            "items": items,
            "assignment": assignment.assignment,
            "snapshot_id": assignment.snapshot_id,
            "request_id": assignment.request_id,
            "direction": direction,
            "strategy_version_id": str(assignment.version.id),
            "algorithm_version": getattr(assignment.version, "algorithm_version", "recommendation-v1"),
            "query_digest": query_digest,
            "candidate_ids": candidate_ids,
            "precision_pool_ids": precision_ids,
            "scoring_time_utc": request_now_utc,
            "llm_status": semantic_result.llm_status,
            "llm_input_tokens": semantic_result.input_tokens,
            "llm_output_tokens": semantic_result.output_tokens,
            "llm_retry_count": semantic_result.retry_count,
            "ranking_fallback": semantic_result.ranking_fallback,
            "ranking_latency_ms": semantic_result.latency_ms,
            "candidate_scores": {
                item.candidate_id: {
                    "final_score": item.repeat_adjusted_score,
                    "is_exploration": item.is_exploration,
                    "reason_codes": list(item.reason_codes),
                    "score_detail": {
                        "match_score": item.match_score,
                        "quality_score": item.quality_score,
                        "freshness_score": item.freshness_score,
                        "exposure_opportunity": item.exposure_opportunity,
                        "base_score": item.base_score,
                        "repeat_factor": item.repeat_factor,
                        "diversity_penalty": item.diversity_penalty,
                    },
                }
                for item in ordered
            },
        }
    except Exception:
        logger.exception("recommendation-v1 failed closed to legacy")
        return None


def _submit_shadow_candidate(
    *,
    candidate_dicts: list[dict],
    direction: str,
    criteria: dict,
    userid: str,
    raw_query: str,
    assignment_decision,
    db: Session,
    request_now_utc: datetime | None = None,
) -> dict | None:
    """Submit the candidate strategy without delaying the legacy serving path.

    The returned metadata is also used to give the served request a stable
    request/snapshot identity.  Persistence remains dormant until Worker commits
    that request and calls ``activate_persistence``.
    """
    shadow_version_id = getattr(assignment_decision, "shadow_version_id", None)
    if not shadow_version_id:
        return None

    query_digest = conversation_service.compute_query_digest(criteria)
    metadata = {
        "request_id": assignment_decision.request_id,
        "snapshot_id": assignment_decision.snapshot_id,
        "query_digest": query_digest,
        "candidate_ids": [
            str(item.get("id")) for item in candidate_dicts[:V1_MAX_CANDIDATES]
        ],
        "precision_pool_ids": [],
        "assignment": _legacy_fallback_assignment(assignment_decision),
    }
    try:
        from app.services.recommendation_exposure_service import (
            batch_candidate_exposures,
            recent_user_exposures,
        )
        from app.services.recommendation_request_service import precision_pool
        from app.services.recommendation_shadow_service import ShadowJob, shadow_runner
        from app.services.recommendation_strategy_service import load_published_version

        version = load_published_version(db, int(shadow_version_id))
        if version is None:
            logger.warning(
                "shadow candidate version unavailable direction=%s version=%s",
                direction,
                shadow_version_id,
            )
            return metadata

        candidates = [dict(item) for item in candidate_dicts[:V1_MAX_CANDIDATES]]
        precision_ids = precision_pool(
            candidates,
            direction=direction,
            criteria=criteria,
            userid=userid,
            query_digest=query_digest,
        )
        metadata["precision_pool_ids"] = precision_ids
        request_now_utc = request_now_utc or utc_now()
        target_type = "job" if direction == "search_job" else "resume"
        try:
            candidate_ids = metadata["candidate_ids"]
            exposure_counts = batch_candidate_exposures(
                db,
                target_type=target_type,
                candidate_ids=candidate_ids,
                request_now_utc=request_now_utc,
            )
            recent_exposures = recent_user_exposures(
                db,
                viewer_userid=userid,
                target_type=target_type,
                candidate_ids=candidate_ids,
                request_now_utc=request_now_utc,
                cooldown_hours=int(version.parameters.get("repeat_cooldown_hours", 24)),
            )
            exposure_available = True
        except Exception:
            logger.warning(
                "shadow exposure read failed; using neutral opportunity",
                exc_info=True,
            )
            exposure_counts, recent_exposures, exposure_available = {}, {}, False

        submitted = time.monotonic()
        handle = shadow_runner.submit(ShadowJob(
            request_id=assignment_decision.request_id,
            direction=direction,
            userid=userid,
            raw_query=raw_query,
            role="worker" if direction == "search_job" else "factory",
            criteria=dict(criteria),
            query_digest=query_digest,
            candidate_dicts=tuple(candidates),
            precision_pool_ids=tuple(precision_ids),
            strategy_version=int(version.id),
            algorithm_version=(
                getattr(version, "algorithm_version", None) or "recommendation-v1"
            ),
            parameters=dict(version.parameters or {}),
            exposure_counts=dict(exposure_counts),
            recent_exposures=dict(recent_exposures),
            exposure_available=exposure_available,
            rotation_date=rotation_date(request_now_utc),
            scoring_time_utc=request_now_utc,
            deadline_monotonic=(
                submitted + float(settings.recommendation_shadow_timeout_seconds)
            ),
            submitted_monotonic=submitted,
            provider=settings.llm_provider,
            daily_token_limit=settings.recommendation_shadow_daily_token_budget(direction),
        ))
        if handle is None:
            logger.warning(
                "shadow runner rejected submission request_id=%s",
                assignment_decision.request_id,
            )
    except Exception:
        # Shadow is observability-only.  Any submission failure leaves the
        # already selected legacy path untouched.
        logger.exception(
            "shadow submission failed request_id=%s",
            assignment_decision.request_id,
        )
    return metadata


def _set_shadow_served_baseline(
    shadow_metadata: dict | None,
    served_ids: list[str],
) -> None:
    if not shadow_metadata:
        return
    try:
        from app.services.recommendation_shadow_service import set_served_baseline

        set_served_baseline(
            shadow_metadata["request_id"],
            served_ids[:V1_DISPLAY_TOP_N],
        )
    except Exception:
        logger.warning("failed to attach shadow served baseline", exc_info=True)


def _attach_legacy_decision_metadata(
    result: SearchResult,
    assignment_decision,
    criteria: dict,
) -> SearchResult:
    """Keep off/shadow/on-fallback facts visible even when no result was served."""
    if assignment_decision is None:
        return result
    result.strategy_assignment = _legacy_fallback_assignment(assignment_decision)
    result.request_id = assignment_decision.request_id
    result.query_digest = conversation_service.compute_query_digest(criteria)
    return result


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def search_jobs(
    criteria: dict,
    raw_query: str,
    session: SessionState,
    user_ctx: UserContext,
    db: Session,
    user_msg_id: str | None = None,
    experience_flags: RecommendationExperienceFlags | None = None,
) -> tuple[SearchResult, SearchOutcome]:
    """工人/中介找岗位。

    Phase 5 §5.0：返回类型从 `SearchResult` 改为 `tuple[SearchResult, SearchOutcome]`，
    本子阶段产出 SearchOutcome 但调用方解构后丢弃；5.1 起 message_router 才开始消费。
    """
    from app.services.recommendation_assignment_service import choose_assignment
    try:
        assignment_decision = choose_assignment(
            db, userid=user_ctx.external_userid, direction="search_job",
        )
        is_v1 = assignment_decision.assignment.assignment != "legacy"
        shadow_due = getattr(assignment_decision, "shadow_version_id", None) is not None
    except Exception:
        logger.warning("recommendation release lookup failed; serving legacy", exc_info=True)
        assignment_decision = None
        is_v1 = False
        shadow_due = False
    # §5.4.1: legacy keeps reading the historical generic config; v1 is pinned to
    # the code constants.  Both are resolved up front so a v1 failure can still
    # fall back to genuine legacy behaviour.
    legacy_top_n = _get_config_int("match.top_n", db, 3)
    legacy_max_candidates = _get_config_int("match.max_candidates", db, 50)
    top_n = V1_DISPLAY_TOP_N if is_v1 else legacy_top_n
    max_candidates = (
        V1_MAX_CANDIDATES if is_v1
        else max(V1_MAX_CANDIDATES, legacy_max_candidates) if shadow_due
        else legacy_max_candidates
    )
    flags = _normalize_experience_flags(experience_flags)

    # 硬过滤
    initial_query_started = time.perf_counter()
    candidates = _query_jobs(criteria, max_candidates, db)
    initial_query_record = _query_attempt_record(
        step="initial",
        criteria=criteria,
        candidates=candidates,
        started=initial_query_started,
    )
    initial_count = len(candidates)

    # Phase 5 §5.2.1 / §5.4：仅 mode=on 且用户命中 rollout 桶时，
    # 低召回才跳过 legacy fallback，由 reducer + post_search_applier 接管。
    # off / shadow / 桶外用户保持旧行为（向后兼容验收 §5.2.4 第 2 项）。
    phase5_takeover = (
        _is_phase5_policy_enabled_for_user(user_ctx.external_userid)
        and len(candidates) < top_n
    )
    available_steps: list[str] = []
    probe_results: list[dict] = []
    if phase5_takeover:
        outcome = FallbackOutcome(candidates=candidates)
        available_steps, probe_results = _probe_relax_steps(
            criteria, "search_job", max_candidates, db,
        )
    elif len(candidates) < top_n:
        # 旧 Stage B fallback：0 命中或低召回时按显式 fallback 步骤逐步放宽
        outcome = _run_job_fallback_steps(
            criteria, candidates, top_n, max_candidates, db,
        )
        candidates = outcome.candidates
        probe_results = list(outcome.probe_results)
        if outcome.applied_step:
            # The selected relaxed query becomes the served auto_relaxed
            # attempt. The original strict query and every non-selected query
            # remain separate facts on the same request.
            probe_results = [initial_query_record] + [
                record for record in probe_results
                if record.get("step") != outcome.applied_step
            ]
            criteria = dict(outcome.applied_criteria or criteria)
    else:
        outcome = FallbackOutcome(candidates=candidates)

    if not candidates:
        if outcome.suggestions:
            sr = SearchResult(
                reply_text=_format_no_match_with_suggestions_job(
                    criteria, outcome.suggestions,
                ),
                result_count=0,
            )
        else:
            sr = SearchResult(
                reply_text=NO_JOB_MATCH_REPLY,
                result_count=0,
            )
        _attach_legacy_decision_metadata(sr, assignment_decision, criteria)
        so = _build_search_outcome(
            direction="search_job",
            criteria_used=criteria,
            initial_count=initial_count,
            final_count=0,
            desired_count=top_n,
            applied_relax_step=outcome.applied_step,
            fallback_suggestions=outcome.suggestions,
            available_relax_steps=available_steps,
            relax_probe_results=probe_results,
        )
        return sr, so

    # 转为 dict 列表用于 rerank
    candidate_dicts = _jobs_to_dicts(candidates, db)

    # Reranker（含结构化打点）
    # Phase 5 §5.3：soft_preference_ranking_enabled=True 时把 criteria 中的
    # 软偏好字段 + ranking_weight 透传给 reranker，让其优先排序匹配软偏好的候选。
    soft_prefs, ranking_weights = _extract_soft_prefs_for_rerank(
        criteria, "job_search", flags,
    )
    request_now_utc = utc_now()
    shadow = _submit_shadow_candidate(
        candidate_dicts=candidate_dicts,
        direction="search_job",
        criteria=criteria,
        userid=user_ctx.external_userid,
        raw_query=raw_query,
        assignment_decision=assignment_decision,
        db=db,
        request_now_utc=request_now_utc,
    ) if shadow_due else None
    # v1 runs first because it owns its own precision-pool LLM call.  Only when
    # it declines (legacy assignment) or fails do we fall back to the legacy
    # reranker — skipping both would hand the user raw SQL `created_at DESC`.
    v1 = _try_recommendation_v1(
        candidate_dicts=candidate_dicts,
        direction="search_job",
        criteria=criteria,
        userid=user_ctx.external_userid,
        db=db,
        raw_query=raw_query,
        assignment_decision=assignment_decision,
        request_now_utc=request_now_utc,
    ) if is_v1 else None
    if v1 is None:
        if is_v1:
            logger.warning("recommendation-v1 unavailable for search_jobs; serving legacy ranking")
            top_n = legacy_top_n
        rerank_result = _rerank_with_logging(
            query=raw_query,
            candidates=candidate_dicts[:legacy_max_candidates],
            role=user_ctx.role,
            top_n=top_n,
            call_site="search_jobs",
            user_msg_id=user_msg_id,
            soft_preferences=soft_prefs,
            ranking_weights=ranking_weights,
        )
        if is_v1 and rerank_result.ranking_fallback is None:
            rerank_result.ranking_fallback = "recommendation_v1_failed"
    else:
        rerank_result = RerankResult(ranked_items=[])

    # 从 rerank 结果提取排序后的 ID 列表（全量快照）
    ranked_ids = list(v1["ids"]) if v1 else [str(item["id"]) for item in rerank_result.ranked_items]
    # 如果 rerank 只返回了 top_n，把剩余候选补到后面
    ranked_id_set = set(ranked_ids)
    for c in candidate_dicts:
        cid = str(c["id"])
        if cid not in ranked_id_set:
            ranked_ids.append(cid)

    # 保存快照
    digest = conversation_service.compute_query_digest(criteria)
    snapshot_kwargs = {}
    if v1:
        snapshot_kwargs = {
            "request_id": v1.get("request_id"),
            "snapshot_id": v1.get("snapshot_id"),
            "direction": v1.get("direction"),
            "strategy_version_id": v1.get("strategy_version_id"),
            "algorithm_version": v1.get("algorithm_version", "recommendation-v1"),
            "assignment": getattr(v1.get("assignment"), "assignment", "legacy"),
            "ranking_metadata": {
                "display_top_n": len(v1.get("items", [])),
                "precision_pool_ids": v1.get("precision_pool_ids", []),
                "candidate_scores": v1.get("candidate_scores", {}),
            },
        }
    elif shadow:
        # Shadow only observes the candidate strategy.  The durable snapshot is
        # the legacy order actually served to the user.
        snapshot_kwargs = {
            "request_id": shadow["request_id"],
            "snapshot_id": shadow["snapshot_id"],
            "direction": "search_job",
            "strategy_version_id": None,
            "algorithm_version": "legacy",
            "assignment": "legacy",
        }
    conversation_service.save_snapshot(session, ranked_ids, digest, criteria, **snapshot_kwargs)

    # 取首批
    first_batch_ids = conversation_service.get_next_candidate_ids(session, top_n)
    if not first_batch_ids:
        sr = SearchResult(
            reply_text="暂无匹配结果。",
            result_count=0,
            snapshot_id=shadow["snapshot_id"] if shadow else None,
            strategy_assignment=(
                shadow["assignment"] if shadow
                else _legacy_fallback_assignment(assignment_decision)
            ),
            request_id=shadow["request_id"] if shadow else (
                assignment_decision.request_id if assignment_decision else None
            ),
            query_digest=shadow["query_digest"] if shadow else digest,
            candidate_ids=shadow["candidate_ids"] if shadow else [],
            precision_pool_ids=shadow["precision_pool_ids"] if shadow else [],
            **_search_attempt_fields(
                v1, rerank_result, scoring_time_utc=request_now_utc,
            ),
        )
        _set_shadow_served_baseline(shadow, [])
        so = _build_search_outcome(
            direction="search_job",
            criteria_used=criteria,
            initial_count=initial_count,
            final_count=0,
            desired_count=top_n,
            applied_relax_step=outcome.applied_step,
            fallback_suggestions=outcome.suggestions,
            available_relax_steps=available_steps,
            relax_probe_results=probe_results,
        )
        return sr, so

    # 从候选中找到对应记录
    id_to_dict = {str(c["id"]): c for c in candidate_dicts}
    batch = [id_to_dict[cid] for cid in first_batch_ids if cid in id_to_dict]

    # 权限过滤
    filtered = permission_service.filter_jobs_batch(batch, user_ctx.role)
    _set_shadow_served_baseline(
        shadow,
        [str(item.get("id")) for item in filtered],
    )

    # 记录已展示
    shown_ids = [str(j["id"]) for j in batch]
    conversation_service.record_shown(session, shown_ids)

    # 格式化
    remaining = conversation_service.get_remaining_count(session)
    reason_lines = _build_job_reason_lines_by_id(
        filtered, criteria, flags, soft_prefs, userid_hash(user_ctx.external_userid),
    )
    reply = _format_job_results(filtered, remaining, reason_lines)
    if outcome.applied_step:
        notice = _FALLBACK_NOTICE_JOB.get(outcome.applied_step)
        if notice:
            reply = f"{notice}\n\n{reply}"

    sr = SearchResult(
        reply_text=reply,
        has_more=remaining > 0,
        result_count=len(filtered),
        recommendation_items=_served_recommendation_items(
            filtered, "search_job", v1["items"] if v1 else None,
        ),
        snapshot_id=(v1["snapshot_id"] if v1 else shadow["snapshot_id"] if shadow else None),
        strategy_assignment=(
            v1["assignment"] if v1 else
            shadow["assignment"] if shadow else
            _legacy_fallback_assignment(assignment_decision)
        ),
        request_id=(v1["request_id"] if v1 else shadow["request_id"] if shadow else None),
        query_digest=(v1["query_digest"] if v1 else shadow["query_digest"] if shadow else digest),
        candidate_ids=(
            v1["candidate_ids"] if v1 else
            shadow["candidate_ids"] if shadow else
            [str(item.get("id")) for item in candidate_dicts[:max_candidates]]
        ),
        precision_pool_ids=(
            v1["precision_pool_ids"] if v1 else
            shadow["precision_pool_ids"] if shadow else []
        ),
        **_search_attempt_fields(
            v1, rerank_result, scoring_time_utc=request_now_utc,
        ),
    )
    # Phase 5 §5.4：统计 ranked_items（即 batch）中各软偏好字段命中数。
    soft_pref_hits = _count_soft_pref_hits(batch, soft_prefs)
    so = _build_search_outcome(
        direction="search_job",
        criteria_used=criteria,
        initial_count=initial_count,
        final_count=len(candidates),
        desired_count=top_n,
        available_relax_steps=available_steps,
        relax_probe_results=probe_results,
        applied_relax_step=outcome.applied_step,
        fallback_suggestions=outcome.suggestions,
        has_more=remaining > 0,
        soft_pref_hits=soft_pref_hits,
        candidate_count_capped=len(candidates),
        visible_count=len(filtered),
        shown_count=len(filtered),
        remaining_count_capped=remaining,
    )
    return sr, so


def search_workers(
    criteria: dict,
    raw_query: str,
    session: SessionState,
    user_ctx: UserContext,
    db: Session,
    user_msg_id: str | None = None,
    experience_flags: RecommendationExperienceFlags | None = None,
) -> tuple[SearchResult, SearchOutcome]:
    """厂家/中介找工人。

    Phase 5 §5.0：返回类型从 `SearchResult` 改为 `tuple[SearchResult, SearchOutcome]`。
    """
    from app.services.recommendation_assignment_service import choose_assignment
    try:
        assignment_decision = choose_assignment(
            db, userid=user_ctx.external_userid, direction="search_worker",
        )
        is_v1 = assignment_decision.assignment.assignment != "legacy"
        shadow_due = getattr(assignment_decision, "shadow_version_id", None) is not None
    except Exception:
        logger.warning("recommendation release lookup failed; serving legacy", exc_info=True)
        assignment_decision = None
        is_v1 = False
        shadow_due = False
    # §5.4.1: legacy keeps reading the historical generic config; v1 is pinned to
    # the code constants.
    legacy_top_n = _get_config_int("match.top_n", db, 3)
    legacy_max_candidates = _get_config_int("match.max_candidates", db, 50)
    top_n = V1_DISPLAY_TOP_N if is_v1 else legacy_top_n
    max_candidates = (
        V1_MAX_CANDIDATES if is_v1
        else max(V1_MAX_CANDIDATES, legacy_max_candidates) if shadow_due
        else legacy_max_candidates
    )
    flags = _normalize_experience_flags(experience_flags)

    initial_query_started = time.perf_counter()
    candidates = _query_resumes(criteria, max_candidates, db)
    initial_query_record = _query_attempt_record(
        step="initial",
        criteria=criteria,
        candidates=candidates,
        started=initial_query_started,
    )
    initial_count = len(candidates)

    # Phase 5 §5.2.1 / §5.4：仅 mode=on 且用户命中 rollout 桶时，
    # 低召回才跳过 legacy fallback，由 reducer + applier 接管放宽决策。
    # off / shadow / 桶外用户保持旧行为。
    phase5_takeover = (
        _is_phase5_policy_enabled_for_user(user_ctx.external_userid)
        and len(candidates) < top_n
    )
    available_steps: list[str] = []
    probe_results: list[dict] = []
    if phase5_takeover:
        outcome = FallbackOutcome(candidates=candidates)
        available_steps, probe_results = _probe_relax_steps(
            criteria, "search_worker", max_candidates, db,
        )
    elif len(candidates) < top_n:
        outcome = _run_resume_fallback_steps(
            criteria, candidates, top_n, max_candidates, db,
        )
        candidates = outcome.candidates
        probe_results = list(outcome.probe_results)
        if outcome.applied_step:
            probe_results = [initial_query_record] + [
                record for record in probe_results
                if record.get("step") != outcome.applied_step
            ]
            criteria = dict(outcome.applied_criteria or criteria)
    else:
        outcome = FallbackOutcome(candidates=candidates)

    if not candidates:
        if outcome.suggestions:
            sr = SearchResult(
                reply_text=_format_no_match_with_suggestions_resume(
                    criteria, outcome.suggestions,
                ),
                result_count=0,
            )
        else:
            sr = SearchResult(
                reply_text=NO_WORKER_MATCH_REPLY,
                result_count=0,
            )
        _attach_legacy_decision_metadata(sr, assignment_decision, criteria)
        so = _build_search_outcome(
            direction="search_worker",
            criteria_used=criteria,
            initial_count=initial_count,
            final_count=0,
            desired_count=top_n,
            applied_relax_step=outcome.applied_step,
            fallback_suggestions=outcome.suggestions,
            available_relax_steps=available_steps,
            relax_probe_results=probe_results,
        )
        return sr, so

    candidate_dicts = _resumes_to_dicts(candidates)

    soft_prefs, ranking_weights = _extract_soft_prefs_for_rerank(
        criteria, "candidate_search", flags,
    )
    request_now_utc = utc_now()
    shadow = _submit_shadow_candidate(
        candidate_dicts=candidate_dicts,
        direction="search_worker",
        criteria=criteria,
        userid=user_ctx.external_userid,
        raw_query=raw_query,
        assignment_decision=assignment_decision,
        db=db,
        request_now_utc=request_now_utc,
    ) if shadow_due else None
    # See search_jobs: v1 first, legacy rerank only as the declined/failed path.
    v1 = _try_recommendation_v1(
        candidate_dicts=candidate_dicts,
        direction="search_worker",
        criteria=criteria,
        userid=user_ctx.external_userid,
        db=db,
        raw_query=raw_query,
        assignment_decision=assignment_decision,
        request_now_utc=request_now_utc,
    ) if is_v1 else None
    if v1 is None:
        if is_v1:
            logger.warning("recommendation-v1 unavailable for search_workers; serving legacy ranking")
            top_n = legacy_top_n
        rerank_result = _rerank_with_logging(
            query=raw_query,
            candidates=candidate_dicts[:legacy_max_candidates],
            role=user_ctx.role,
            top_n=top_n,
            call_site="search_workers",
            user_msg_id=user_msg_id,
            soft_preferences=soft_prefs,
            ranking_weights=ranking_weights,
        )
        if is_v1 and rerank_result.ranking_fallback is None:
            rerank_result.ranking_fallback = "recommendation_v1_failed"
    else:
        rerank_result = RerankResult(ranked_items=[])

    ranked_ids = list(v1["ids"]) if v1 else [str(item["id"]) for item in rerank_result.ranked_items]
    ranked_id_set = set(ranked_ids)
    for c in candidate_dicts:
        cid = str(c["id"])
        if cid not in ranked_id_set:
            ranked_ids.append(cid)

    digest = conversation_service.compute_query_digest(criteria)
    snapshot_kwargs = {}
    if v1:
        snapshot_kwargs = {
            "request_id": v1.get("request_id"),
            "snapshot_id": v1.get("snapshot_id"),
            "direction": v1.get("direction"),
            "strategy_version_id": v1.get("strategy_version_id"),
            "algorithm_version": v1.get("algorithm_version", "recommendation-v1"),
            "assignment": getattr(v1.get("assignment"), "assignment", "legacy"),
            "ranking_metadata": {
                "display_top_n": len(v1.get("items", [])),
                "precision_pool_ids": v1.get("precision_pool_ids", []),
                "candidate_scores": v1.get("candidate_scores", {}),
            },
        }
    elif shadow:
        snapshot_kwargs = {
            "request_id": shadow["request_id"],
            "snapshot_id": shadow["snapshot_id"],
            "direction": "search_worker",
            "strategy_version_id": None,
            "algorithm_version": "legacy",
            "assignment": "legacy",
        }
    conversation_service.save_snapshot(session, ranked_ids, digest, criteria, **snapshot_kwargs)

    first_batch_ids = conversation_service.get_next_candidate_ids(session, top_n)
    if not first_batch_ids:
        sr = SearchResult(
            reply_text="暂无匹配结果。",
            result_count=0,
            snapshot_id=shadow["snapshot_id"] if shadow else None,
            strategy_assignment=(
                shadow["assignment"] if shadow
                else _legacy_fallback_assignment(assignment_decision)
            ),
            request_id=shadow["request_id"] if shadow else (
                assignment_decision.request_id if assignment_decision else None
            ),
            query_digest=shadow["query_digest"] if shadow else digest,
            candidate_ids=shadow["candidate_ids"] if shadow else [],
            precision_pool_ids=shadow["precision_pool_ids"] if shadow else [],
            **_search_attempt_fields(
                v1, rerank_result, scoring_time_utc=request_now_utc,
            ),
        )
        _set_shadow_served_baseline(shadow, [])
        so = _build_search_outcome(
            direction="search_worker",
            criteria_used=criteria,
            initial_count=initial_count,
            final_count=0,
            desired_count=top_n,
            applied_relax_step=outcome.applied_step,
            fallback_suggestions=outcome.suggestions,
            available_relax_steps=available_steps,
            relax_probe_results=probe_results,
        )
        return sr, so

    id_to_dict = {str(c["id"]): c for c in candidate_dicts}
    batch = [id_to_dict[cid] for cid in first_batch_ids if cid in id_to_dict]

    # 构建 users_map 用于权限过滤
    owner_ids = list({r.get("owner_userid", "") for r in batch})
    users_map = _build_users_map(owner_ids, db)
    filtered = permission_service.filter_resumes_batch(batch, users_map, user_ctx.role)
    _set_shadow_served_baseline(
        shadow,
        [str(item.get("id")) for item in filtered],
    )

    shown_ids = [str(r["id"]) for r in batch]
    conversation_service.record_shown(session, shown_ids)

    remaining = conversation_service.get_remaining_count(session)
    reason_lines = _build_resume_reason_lines_by_id(
        filtered, criteria, flags, soft_prefs, userid_hash(user_ctx.external_userid),
    )
    reply = _format_resume_results(filtered, remaining, reason_lines)
    if outcome.applied_step:
        notice = _FALLBACK_NOTICE_RESUME.get(outcome.applied_step)
        if notice:
            reply = f"{notice}\n\n{reply}"

    sr = SearchResult(
        reply_text=reply,
        has_more=remaining > 0,
        result_count=len(filtered),
        recommendation_items=_served_recommendation_items(
            filtered, "search_worker", v1["items"] if v1 else None,
        ),
        snapshot_id=(v1["snapshot_id"] if v1 else shadow["snapshot_id"] if shadow else None),
        strategy_assignment=(
            v1["assignment"] if v1 else
            shadow["assignment"] if shadow else
            _legacy_fallback_assignment(assignment_decision)
        ),
        request_id=(v1["request_id"] if v1 else shadow["request_id"] if shadow else None),
        query_digest=(v1["query_digest"] if v1 else shadow["query_digest"] if shadow else digest),
        candidate_ids=(
            v1["candidate_ids"] if v1 else
            shadow["candidate_ids"] if shadow else
            [str(item.get("id")) for item in candidate_dicts[:max_candidates]]
        ),
        precision_pool_ids=(
            v1["precision_pool_ids"] if v1 else
            shadow["precision_pool_ids"] if shadow else []
        ),
        **_search_attempt_fields(
            v1, rerank_result, scoring_time_utc=request_now_utc,
        ),
    )
    # Phase 5 §5.4：统计 ranked_items 中各软偏好字段命中数。
    soft_pref_hits = _count_soft_pref_hits(batch, soft_prefs)
    so = _build_search_outcome(
        direction="search_worker",
        criteria_used=criteria,
        initial_count=initial_count,
        final_count=len(candidates),
        desired_count=top_n,
        applied_relax_step=outcome.applied_step,
        fallback_suggestions=outcome.suggestions,
        has_more=remaining > 0,
        soft_pref_hits=soft_pref_hits,
        available_relax_steps=available_steps,
        relax_probe_results=probe_results,
        candidate_count_capped=len(candidates),
        visible_count=len(filtered),
        shown_count=len(filtered),
        remaining_count_capped=remaining,
    )
    return sr, so


def show_more(
    session: SessionState,
    user_ctx: UserContext,
    db: Session,
    experience_flags: RecommendationExperienceFlags | None = None,
) -> tuple[SearchResult, SearchOutcome]:
    """show_more：从快照取下一批，跳过失效条目。

    Phase 5 §5.0：返回类型从 `SearchResult` 改为 `tuple[SearchResult, SearchOutcome]`。
    `SearchResult.reply_text` 完全保留旧文案（含"已经是所有匹配结果了..."兜底），
    off / shadow 模式下逐字节等价 5.0 前的输出；on 模式下由 5.1 reducer + applier
    根据 `SearchOutcome.snapshot_exhausted` 决定是否覆盖 reply。
    """
    is_job_search = _is_job_search(session, user_ctx)
    direction = "search_job" if is_job_search else "search_worker"
    # §5.4.1: a v1 snapshot is pinned to the fixed page size of 3.  Reading the
    # legacy `match.top_n` here made paging drift off the v1 contract whenever
    # the historical production value was not 3.
    snapshot_is_v1 = _snapshot_is_v1(session.candidate_snapshot)
    top_n = V1_DISPLAY_TOP_N if snapshot_is_v1 else _get_config_int("match.top_n", db, 3)
    flags = _normalize_experience_flags(experience_flags)

    # §7.5: while the kill switch is on, a v1 snapshot must be invalidated
    # immediately rather than continuing to page out a v1 ordering that the
    # operator has just disabled.
    if snapshot_is_v1 and _recommendation_kill_switch(db):
        session.candidate_snapshot = None
        session.shown_items = []
        logger.warning("recommendation kill switch active; invalidated v1 snapshot for show_more")
        sr = SearchResult(reply_text="搜索结果已过期，请重新搜索。")
        so = _build_search_outcome(
            direction=direction,
            criteria_used={},
            initial_count=0,
            final_count=0,
            desired_count=top_n,
        )
        return sr, so

    if session.candidate_snapshot is None:
        sr = SearchResult(reply_text="当前没有可以继续查看的结果，请先搜索。")
        so = _build_search_outcome(
            direction=direction,
            criteria_used={},
            initial_count=0,
            final_count=0,
            desired_count=top_n,
        )
        return sr, so

    # 快照过期检查
    if conversation_service.invalidate_snapshot_if_expired(session):
        sr = SearchResult(reply_text="搜索结果已过期，请重新搜索。")
        so = _build_search_outcome(
            direction=direction,
            criteria_used={},
            initial_count=0,
            final_count=0,
            desired_count=top_n,
        )
        return sr, so

    snapshot_effective_criteria = getattr(
        session.candidate_snapshot, "effective_criteria", None,
    )
    effective_criteria = dict(snapshot_effective_criteria or {})

    collected = []
    attempts = 0
    max_attempts = top_n * 3  # 防止无限循环

    while len(collected) < top_n and attempts < max_attempts:
        attempts += 1
        batch_ids = conversation_service.get_next_candidate_ids(
            session, top_n - len(collected),
        )
        if not batch_ids:
            break

        # 标记为已展示（即使失效也要标记，避免重复取）
        conversation_service.record_shown(session, batch_ids)

        if is_job_search:
            # 重新查询验证有效性
            valid = _validate_job_ids(batch_ids, db)
            valid_dicts = _jobs_to_dicts(valid, db)
            filtered = permission_service.filter_jobs_batch(valid_dicts, user_ctx.role)
        else:
            valid = _validate_resume_ids(batch_ids, db)
            valid_dicts = _resumes_to_dicts(valid)
            owner_ids = list({r.get("owner_userid", "") for r in valid_dicts})
            users_map = _build_users_map(owner_ids, db)
            filtered = permission_service.filter_resumes_batch(
                valid_dicts, users_map, user_ctx.role,
            )

        collected.extend(filtered)

    if not collected:
        log_event(
            "show_more_exhausted",
            external_userid_hash=userid_hash(user_ctx.external_userid),
            direction=direction,
            remaining_count_capped=0,
            snapshot_has_effective_criteria=bool(snapshot_effective_criteria),
            active_flow=session.active_flow or "",
        )
        sr = SearchResult(
            reply_text="已经是所有匹配结果了。要不要调整条件重新搜索？",
            result_count=0,
        )
        so = _build_search_outcome(
            direction=direction,
            criteria_used=effective_criteria,
            initial_count=0,
            final_count=0,
            desired_count=top_n,
            snapshot_exhausted=True,  # 5.1 reducer 据此决定 paginate_no_more
            visible_count=0,
            shown_count=0,
            remaining_count_capped=0,
        )
        return sr, so

    # 截断到 top_n
    collected = collected[:top_n]
    remaining = conversation_service.get_remaining_count(session)
    has_more = remaining > 0

    if is_job_search:
        soft_prefs, _ = _extract_soft_prefs_for_rerank(
            effective_criteria, "job_search", flags,
        )
        reason_lines = _build_job_reason_lines_by_id(
            collected,
            effective_criteria,
            flags,
            soft_prefs,
            userid_hash(user_ctx.external_userid),
        )
        reply = _format_job_results(collected, remaining, reason_lines)
    else:
        soft_prefs, _ = _extract_soft_prefs_for_rerank(
            effective_criteria, "candidate_search", flags,
        )
        reason_lines = _build_resume_reason_lines_by_id(
            collected,
            effective_criteria,
            flags,
            soft_prefs,
            userid_hash(user_ctx.external_userid),
        )
        reply = _format_resume_results(collected, remaining, reason_lines)

    sr = SearchResult(
        reply_text=reply,
        has_more=has_more,
        result_count=len(collected),
    )
    snapshot = session.candidate_snapshot
    if snapshot and snapshot.algorithm_version != "legacy" and snapshot.assignment != "legacy":
        from app.schemas.recommendation import (
            RecommendationItem,
            RecommendationScoreDetail,
            StrategyAssignment,
        )
        assignment = StrategyAssignment(
            direction=direction,
            execution_mode="on",
            assignment=snapshot.assignment,
            strategy_version_id=snapshot.strategy_version_id,
            algorithm_version=snapshot.algorithm_version,
        )
        score_map = snapshot.ranking_metadata.get("candidate_scores", {})
        recommendation_items = []
        for index, item in enumerate(collected, 1):
            score = dict(score_map.get(str(item["id"])) or {})
            detail = score.get("score_detail")
            recommendation_items.append(RecommendationItem(
                target_type="job" if direction == "search_job" else "resume",
                target_id=int(item["id"]),
                position=index,
                final_score=float(score.get("final_score", 0.0)),
                is_exploration=bool(score.get("is_exploration", False)),
                reason_codes=list(score.get("reason_codes") or []),
                score_detail=(
                    RecommendationScoreDetail.model_validate(detail)
                    if detail else None
                ),
            ))
        sr.recommendation_items = recommendation_items
        sr.snapshot_id = snapshot.snapshot_id
        sr.request_id = snapshot.request_id
        sr.query_digest = snapshot.query_digest
        sr.strategy_assignment = assignment
        sr.candidate_ids = list(snapshot.candidate_ids)
        sr.precision_pool_ids = list(snapshot.ranking_metadata.get("precision_pool_ids", []))
    so = _build_search_outcome(
        direction=direction,
        criteria_used=effective_criteria,
        initial_count=len(collected),
        final_count=len(collected),
        desired_count=top_n,
        has_more=has_more,
        visible_count=len(collected),
        shown_count=len(collected),
        remaining_count_capped=remaining,
    )
    return sr, so


# ---------------------------------------------------------------------------
# 硬过滤查询
# ---------------------------------------------------------------------------

def has_effective_search_criteria(criteria: dict) -> bool:
    """Stage A 搜索安全护栏：city / job_category 至少一个非空才允许查询。

    任何无 city/job_category 的 criteria（例如只含 headcount）都视为无效，
    上层应跳过 SQL 查询直接返回空结果，避免全表召回。
    """
    if not criteria:
        return False
    return bool(criteria.get("city") or criteria.get("job_category"))


def _query_jobs(criteria: dict, limit: int, db: Session) -> list:
    """构建岗位硬过滤查询。"""
    if not has_effective_search_criteria(criteria):
        return []
    now = datetime.now(timezone.utc)
    q = db.query(Job).join(User, Job.owner_userid == User.external_userid).filter(
        Job.audit_status == "passed",
        Job.deleted_at.is_(None),
        Job.expires_at > now,
        Job.delist_reason.is_(None),
        User.status == "active",
    )

    # 业务条件
    cities = criteria.get("city", [])
    if cities:
        if isinstance(cities, list):
            q = q.filter(Job.city.in_(cities))
        else:
            q = q.filter(Job.city == cities)

    categories = criteria.get("job_category", [])
    if categories:
        if isinstance(categories, list):
            q = q.filter(Job.job_category.in_(categories))
        else:
            q = q.filter(Job.job_category == categories)

    salary_floor = criteria.get("salary_floor_monthly")
    if salary_floor is not None:
        q = q.filter(_job_salary_covers_floor(salary_floor))

    is_long_term = criteria.get("is_long_term")
    if is_long_term is not None:
        q = q.filter(Job.is_long_term == is_long_term)

    # 可选过滤开关（从 system_config 读取）
    gender = criteria.get("gender_required")
    if gender and _get_config_bool("filter.enable_gender", db, True):
        q = q.filter(Job.gender_required.in_([gender, "不限"]))

    age = criteria.get("age")
    if age is not None and _get_config_bool("filter.enable_age", db, True):
        q = q.filter(sa.or_(Job.age_min.is_(None), Job.age_min <= age))
        q = q.filter(sa.or_(Job.age_max.is_(None), Job.age_max >= age))

    # 排序 + 截断
    q = q.order_by(Job.created_at.desc(), Job.id.desc())
    return q.limit(limit).all()


def _query_resumes(criteria: dict, limit: int, db: Session) -> list:
    """构建简历硬过滤查询。"""
    if not has_effective_search_criteria(criteria):
        return []
    now = datetime.now(timezone.utc)
    q = db.query(Resume).join(
        User, Resume.owner_userid == User.external_userid,
    ).filter(
        Resume.audit_status == "passed",
        Resume.deleted_at.is_(None),
        Resume.expires_at > now,
        User.status == "active",
    )

    # 城市：检索条件的 city 需要与简历的 expected_cities JSON 数组匹配
    # 使用 JSON_CONTAINS + OR 逻辑：简历期望城市包含搜索条件中的任一城市即命中
    cities = criteria.get("city", [])
    if cities:
        if isinstance(cities, str):
            cities = [cities]
        city_filters = [
            sa.func.json_contains(
                Resume.expected_cities,
                _json_scalar(city),
            )
            for city in cities
        ]
        if city_filters:
            q = q.filter(sa.or_(*city_filters))

    categories = criteria.get("job_category", [])
    if categories:
        if isinstance(categories, str):
            categories = [categories]
        cat_filters = [
            sa.func.json_contains(
                Resume.expected_job_categories,
                _json_scalar(cat),
            )
            for cat in categories
        ]
        if cat_filters:
            q = q.filter(sa.or_(*cat_filters))

    salary_ceiling = criteria.get("salary_ceiling_monthly")
    if salary_ceiling is not None:
        q = q.filter(Resume.salary_expect_floor_monthly <= salary_ceiling)

    # 可选过滤开关
    gender = criteria.get("gender")
    if gender and _get_config_bool("filter.enable_gender", db, True):
        q = q.filter(Resume.gender == gender)

    age = criteria.get("age")
    if age is not None and _get_config_bool("filter.enable_age", db, True):
        q = q.filter(Resume.age == age)

    q = q.order_by(Resume.created_at.desc(), Resume.id.desc())
    return q.limit(limit).all()


# ---------------------------------------------------------------------------
# Stage B：显式 fallback 步骤（§3.4）
# ---------------------------------------------------------------------------
#
# 步骤设计原则：
# 1. 每一步都必须保留 city / job_category 守卫，禁止全表召回。
# 2. 每一步都打 ``search_fallback_applied`` 日志，含 step / 候选数 / criteria 概要。
# 3. 命中数比上一步多才采用结果；否则丢弃，避免为了"更多"返回低质量结果。
# 4. 同省/周边城市依赖城市字典，Stage B 不实现，留作后续扩展。

# 可选硬过滤字段：在 0/低召回时被允许去掉的字段（保留 city / job_category /
# salary_*）。这与 §3.4 Step 2 “去可选硬过滤”对应。
_OPTIONAL_HARD_FILTERS_JOB = ("gender_required", "is_long_term", "age")
_OPTIONAL_HARD_FILTERS_RESUME = ("gender", "age")


def _run_job_fallback_steps(
    criteria: dict,
    initial: list,
    top_n: int,
    limit: int,
    db: Session,
) -> FallbackOutcome:
    """岗位搜索 0/低召回时的分步 fallback。

    Step 1: 薪资下限放宽 10%
    Step 2: 工种细分类/口语化值 → canonical 大类（spec §3.4 Step 3）
    Step 3: 去掉可选硬过滤（gender / is_long_term / age），叠加薪资放宽

    Bug 3：返回结构化 FallbackOutcome，包含 applied_step 用于 reply 前缀；
    若所有温和步骤仍 0 召回，再激进探查并产出 suggestions。
    """
    best = initial
    applied_step: str | None = None
    applied_criteria: dict | None = None
    probe_results: list[dict] = []
    steps: list[tuple[str, dict]] = []

    salary = criteria.get("salary_floor_monthly")
    if salary is not None:
        relaxed_salary = dict(criteria)
        relaxed_salary["salary_floor_monthly"] = math.floor(int(salary) * 0.9)
        steps.append(("relax_salary_10pct", relaxed_salary))

    broadened = _broaden_job_categories(criteria)
    if broadened is not None:
        steps.append(("broaden_job_category", broadened))

    drop_optional = _strip_optional_filters(criteria, _OPTIONAL_HARD_FILTERS_JOB)
    if drop_optional != criteria:
        # 同时叠加薪资放宽和大类放宽，最大化命中
        if salary is not None:
            drop_optional["salary_floor_monthly"] = math.floor(int(salary) * 0.9)
        broadened_drop = _broaden_job_categories(drop_optional)
        if broadened_drop is not None:
            drop_optional = broadened_drop
        steps.append(("drop_optional_filters", drop_optional))

    for step_name, step_criteria in steps:
        started = time.perf_counter()
        candidates = _query_jobs(step_criteria, limit, db)
        probe_results.append(_query_attempt_record(
            step=step_name,
            criteria=step_criteria,
            candidates=candidates,
            started=started,
        ))
        log_event(
            "search_fallback_applied",
            direction="search_job",
            step=step_name,
            candidate_count=len(candidates),
            previous_count=len(best),
            criteria_keys=sorted(step_criteria.keys()),
        )
        if len(candidates) > len(best):
            best = candidates
            applied_step = step_name
            applied_criteria = dict(step_criteria)
            if len(best) >= top_n:
                break

    suggestions: list[FallbackSuggestion] = []
    if not best:
        suggestions = _probe_job_suggestions(
            criteria, limit, db, attempt_records=probe_results,
        )

    return FallbackOutcome(
        candidates=best,
        applied_step=applied_step,
        applied_criteria=applied_criteria,
        probe_results=probe_results,
        suggestions=suggestions,
    )


def _run_resume_fallback_steps(
    criteria: dict,
    initial: list,
    top_n: int,
    limit: int,
    db: Session,
) -> FallbackOutcome:
    """简历搜索 0/低召回时的分步 fallback。

    Step 1: 薪资上限放宽 10%
    Step 2: 工种细分类/口语化值 → canonical 大类（spec §3.4 Step 3）
    Step 3: 去掉可选硬过滤（gender / age），叠加薪资放宽
    """
    best = initial
    applied_step: str | None = None
    applied_criteria: dict | None = None
    probe_results: list[dict] = []
    steps: list[tuple[str, dict]] = []

    salary = criteria.get("salary_ceiling_monthly")
    if salary is not None:
        relaxed_salary = dict(criteria)
        relaxed_salary["salary_ceiling_monthly"] = math.ceil(int(salary) * 1.1)
        steps.append(("relax_salary_10pct", relaxed_salary))

    broadened = _broaden_job_categories(criteria)
    if broadened is not None:
        steps.append(("broaden_job_category", broadened))

    drop_optional = _strip_optional_filters(criteria, _OPTIONAL_HARD_FILTERS_RESUME)
    if drop_optional != criteria:
        if salary is not None:
            drop_optional["salary_ceiling_monthly"] = math.ceil(int(salary) * 1.1)
        broadened_drop = _broaden_job_categories(drop_optional)
        if broadened_drop is not None:
            drop_optional = broadened_drop
        steps.append(("drop_optional_filters", drop_optional))

    for step_name, step_criteria in steps:
        started = time.perf_counter()
        candidates = _query_resumes(step_criteria, limit, db)
        probe_results.append(_query_attempt_record(
            step=step_name,
            criteria=step_criteria,
            candidates=candidates,
            started=started,
        ))
        log_event(
            "search_fallback_applied",
            direction="search_worker",
            step=step_name,
            candidate_count=len(candidates),
            previous_count=len(best),
            criteria_keys=sorted(step_criteria.keys()),
        )
        if len(candidates) > len(best):
            best = candidates
            applied_step = step_name
            applied_criteria = dict(step_criteria)
            if len(best) >= top_n:
                break

    suggestions: list[FallbackSuggestion] = []
    if not best:
        suggestions = _probe_resume_suggestions(
            criteria, limit, db, attempt_records=probe_results,
        )

    return FallbackOutcome(
        candidates=best,
        applied_step=applied_step,
        applied_criteria=applied_criteria,
        probe_results=probe_results,
        suggestions=suggestions,
    )


# ---------------------------------------------------------------------------
# Phase 5 §5.2：放宽步骤探查 + 显式 step 二次检索
# ---------------------------------------------------------------------------

# 5.2 reducer 据此知道"哪些 step 还能放宽"；step 名与 _FALLBACK_NOTICE_* 对齐，
# 避免再写一份内部常量。
_RELAX_STEP_NAMES_JOB = ("relax_salary_10pct", "broaden_job_category", "drop_optional_filters")
_RELAX_STEP_NAMES_RESUME = ("relax_salary_10pct", "broaden_job_category", "drop_optional_filters")


def _compute_relaxed_criteria_job(original: dict, step: str) -> dict:
    """phased-plan §5.2.1 第 4 项：按 step 名一次性算放宽后的 criteria。

    与 _run_job_fallback_steps 内部分支语义一致；execute_relaxed_search 调本
    helper 算一次，**不**让外部传入 relaxed_criteria（避免二次放宽风险）。
    """
    if step == "relax_salary_10pct":
        salary = original.get("salary_floor_monthly")
        if salary is None:
            return dict(original)
        out = dict(original)
        out["salary_floor_monthly"] = math.floor(int(salary) * 0.9)
        return out
    if step == "broaden_job_category":
        broadened = _broaden_job_categories(original)
        return broadened if broadened is not None else dict(original)
    if step == "drop_optional_filters":
        out = _strip_optional_filters(original, _OPTIONAL_HARD_FILTERS_JOB)
        salary = original.get("salary_floor_monthly")
        if salary is not None:
            out["salary_floor_monthly"] = math.floor(int(salary) * 0.9)
        broadened = _broaden_job_categories(out)
        if broadened is not None:
            out = broadened
        return out
    return dict(original)


def _compute_relaxed_criteria_resume(original: dict, step: str) -> dict:
    """phased-plan §5.2.1 第 4 项：candidate_search 视角按 step 算放宽。"""
    if step == "relax_salary_10pct":
        salary = original.get("salary_ceiling_monthly")
        if salary is None:
            return dict(original)
        out = dict(original)
        out["salary_ceiling_monthly"] = math.ceil(int(salary) * 1.1)
        return out
    if step == "broaden_job_category":
        broadened = _broaden_job_categories(original)
        return broadened if broadened is not None else dict(original)
    if step == "drop_optional_filters":
        out = _strip_optional_filters(original, _OPTIONAL_HARD_FILTERS_RESUME)
        salary = original.get("salary_ceiling_monthly")
        if salary is not None:
            out["salary_ceiling_monthly"] = math.ceil(int(salary) * 1.1)
        broadened = _broaden_job_categories(out)
        if broadened is not None:
            out = broadened
        return out
    return dict(original)


def _probe_relax_steps(
    criteria: dict,
    direction: str,
    limit: int,
    db: Session,
) -> tuple[list[str], list[dict]]:
    """phased-plan §5.2.3 search_service 行：探查每步放宽下的候选数。

    返回 ``(available_step_names, probe_results)``：
    - available_step_names：仍然适用的 step 名（如 salary 字段为 None 时
      ``relax_salary_10pct`` 不进入）；
    - probe_results：``[{"step": ..., "count": ...}, ...]`` 给 reducer 决策参考。

    注意：本函数会跑 SQL，但只在低召回 / 0 召回时才被调用，且 limit 小（max_candidates）。
    """
    if direction == "search_job":
        all_steps = _RELAX_STEP_NAMES_JOB
        compute = _compute_relaxed_criteria_job
        query_fn = _query_jobs
    else:
        all_steps = _RELAX_STEP_NAMES_RESUME
        compute = _compute_relaxed_criteria_resume
        query_fn = _query_resumes

    available: list[str] = []
    probes: list[dict] = []
    for step in all_steps:
        relaxed = compute(criteria, step)
        if relaxed == criteria:
            # 该 step 不会改变 criteria（如薪资字段缺失 → relax_salary_10pct 无效）
            continue
        started = time.perf_counter()
        candidates = query_fn(relaxed, limit, db)
        available.append(step)
        record = _query_attempt_record(
            step=step,
            criteria=relaxed,
            candidates=candidates,
            started=started,
        )
        # Reducer compatibility: it consumes ``count`` while persistence uses
        # the explicit candidate_count/result_count fields.
        record["count"] = len(candidates)
        probes.append(record)
    return available, probes


def execute_relaxed_search(
    original_criteria: dict,
    step: str,
    *,
    direction: Literal["search_job", "search_worker"],
    raw_query: str,
    session: SessionState,
    user_ctx: UserContext,
    db: Session,
    user_msg_id: str | None = None,
    experience_flags: RecommendationExperienceFlags | None = None,
    original_visible_count: int = 0,
) -> tuple[SearchResult, SearchOutcome]:
    """phased-plan §5.2.1 第 4 项：用户确认放宽后的二次检索。

    第一参数 **必须** 是 original_criteria（未放宽），由本函数内部按 step
    **一次性** 计算放宽 criteria。**不允许** 调用方传 relaxed_criteria（会
    导致二次放宽，详见 phased-plan §5.2.4 验收 #6 grep 守护）。

    内部链路与 search_jobs/search_workers 一致：硬过滤 → reranker → 快照 →
    权限过滤 → 文案渲染。**不再走** ``_run_*_fallback_steps`` 级联（reducer
    第二轮已由 recursion_depth=1 守护，不允许再次输出 auto_relax_and_retry）。
    """
    if direction == "search_job":
        relaxed = _compute_relaxed_criteria_job(original_criteria, step)
    else:
        relaxed = _compute_relaxed_criteria_resume(original_criteria, step)

    relaxed_decision = None
    try:
        from app.services.recommendation_assignment_service import choose_assignment
        relaxed_decision = choose_assignment(
            db, userid=user_ctx.external_userid, direction=direction,
        )
    except Exception:
        logger.warning("recommendation release lookup failed; serving legacy", exc_info=True)
    relaxed_is_v1 = bool(
        relaxed_decision
        and relaxed_decision.version
        and relaxed_decision.assignment.assignment != "legacy"
    )
    shadow_due = bool(
        relaxed_decision
        and getattr(relaxed_decision, "shadow_version_id", None) is not None
    )
    legacy_top_n = _get_config_int("match.top_n", db, 3)
    legacy_max_candidates = _get_config_int("match.max_candidates", db, 50)
    top_n = V1_DISPLAY_TOP_N if relaxed_is_v1 else legacy_top_n
    max_candidates = (
        V1_MAX_CANDIDATES if relaxed_is_v1
        else max(V1_MAX_CANDIDATES, legacy_max_candidates) if shadow_due
        else legacy_max_candidates
    )
    flags = _normalize_experience_flags(experience_flags)

    if direction == "search_job":
        candidates = _query_jobs(relaxed, max_candidates, db)
    else:
        candidates = _query_resumes(relaxed, max_candidates, db)
    initial_count = len(candidates)

    if not candidates:
        # 二次检索仍然 0 命中（极少；用户已经接受放宽了，不再继续放宽）
        no_match_reply = (
            NO_JOB_MATCH_REPLY if direction == "search_job"
            else NO_WORKER_MATCH_REPLY
        )
        summary = RelaxationSummary(
            field=step,
            label=_relax_step_label(direction, step),
            original_criteria=dict(original_criteria or {}),
            relaxed_criteria=dict(relaxed or {}),
            original_visible_count=original_visible_count,
            relaxed_visible_count=0,
            relaxed_shown_count=0,
        )
        reply = f"{_render_relaxation_summary_notice(summary)}\n\n{no_match_reply}"
        log_event(
            "auto_relax_applied",
            external_userid_hash=userid_hash(user_ctx.external_userid),
            direction=direction,
            field=step,
            original_visible_count=original_visible_count,
            relaxed_visible_count=0,
            relaxed_shown_count=0,
            applied=True,
        )
        sr = SearchResult(reply_text=reply, result_count=0)
        _attach_legacy_decision_metadata(sr, relaxed_decision, relaxed)
        so = _build_search_outcome(
            direction=direction,
            criteria_used=relaxed,
            initial_count=initial_count,
            final_count=0,
            desired_count=top_n,
            applied_relax_step=step,
            relaxation_summary=summary,
        )
        return sr, so

    if direction == "search_job":
        candidate_dicts = _jobs_to_dicts(candidates, db)
    else:
        candidate_dicts = _resumes_to_dicts(candidates)

    frame = "candidate_search" if direction == "search_worker" else "job_search"
    soft_prefs, ranking_weights = _extract_soft_prefs_for_rerank(relaxed, frame, flags)
    digest = conversation_service.compute_query_digest(relaxed)
    request_now_utc = utc_now()
    shadow = _submit_shadow_candidate(
        candidate_dicts=candidate_dicts,
        direction=direction,
        criteria=relaxed,
        userid=user_ctx.external_userid,
        raw_query=raw_query,
        assignment_decision=relaxed_decision,
        db=db,
        request_now_utc=request_now_utc,
    ) if shadow_due else None

    # The relaxed path shares the exact v1 entry point used by the initial
    # search, so it inherits the §6.2 "LLM input = precision Top 20" contract and
    # the §6.3.4 algorithm isolation.  Previously it reranked the whole pool with
    # the *legacy* soft-preference gate and fed those ranks into v1 as semantic
    # scores, which broke both contracts at once.
    v1_meta = _try_recommendation_v1(
        candidate_dicts=candidate_dicts,
        direction=direction,
        criteria=relaxed,
        userid=user_ctx.external_userid,
        db=db,
        raw_query=raw_query,
        assignment_decision=relaxed_decision,
        request_now_utc=request_now_utc,
    ) if relaxed_is_v1 else None

    if v1_meta is None:
        if relaxed_is_v1:
            logger.warning(
                "recommendation-v1 unavailable for execute_relaxed_search:%s; "
                "serving legacy ranking", direction,
            )
            top_n = legacy_top_n
        rerank_result = _rerank_with_logging(
            query=raw_query,
            candidates=candidate_dicts[:legacy_max_candidates],
            role=user_ctx.role,
            top_n=top_n,
            call_site=f"execute_relaxed_search:{direction}",
            user_msg_id=user_msg_id,
            soft_preferences=soft_prefs,
            ranking_weights=ranking_weights,
        )
        if relaxed_is_v1 and rerank_result.ranking_fallback is None:
            rerank_result.ranking_fallback = "recommendation_v1_failed"
        ranked_ids = [str(item["id"]) for item in rerank_result.ranked_items]
    else:
        rerank_result = RerankResult(ranked_items=[])
        ranked_ids = list(v1_meta["ids"])
    ranked_id_set = set(ranked_ids)
    for c in candidate_dicts:
        cid = str(c["id"])
        if cid not in ranked_id_set:
            ranked_ids.append(cid)

    conversation_service.save_snapshot(
        session, ranked_ids, digest, relaxed,
        request_id=(
            v1_meta["request_id"] if v1_meta else
            shadow["request_id"] if shadow else None
        ),
        snapshot_id=(
            v1_meta["snapshot_id"] if v1_meta else
            shadow["snapshot_id"] if shadow else None
        ),
        direction=direction if (v1_meta or shadow) else None,
        strategy_version_id=(
            v1_meta["assignment"].strategy_version_id if v1_meta else None
        ),
        algorithm_version=(
            v1_meta["assignment"].algorithm_version if v1_meta else "legacy"
        ),
        assignment=(
            v1_meta["assignment"].assignment if v1_meta else "legacy"
        ),
        ranking_metadata={
            "precision_pool_ids": v1_meta["precision_pool_ids"],
            "candidate_scores": v1_meta["candidate_scores"],
        } if v1_meta else {},
    )

    first_batch_ids = conversation_service.get_next_candidate_ids(session, top_n)
    if not first_batch_ids:
        sr = SearchResult(
            reply_text="暂无匹配结果。",
            result_count=0,
            snapshot_id=shadow["snapshot_id"] if shadow else None,
            strategy_assignment=(
                shadow["assignment"] if shadow
                else _legacy_fallback_assignment(relaxed_decision)
            ),
            request_id=shadow["request_id"] if shadow else (
                relaxed_decision.request_id if relaxed_decision else None
            ),
            query_digest=shadow["query_digest"] if shadow else digest,
            candidate_ids=shadow["candidate_ids"] if shadow else [],
            precision_pool_ids=shadow["precision_pool_ids"] if shadow else [],
        )
        _set_shadow_served_baseline(shadow, [])
        so = _build_search_outcome(
            direction=direction,
            criteria_used=relaxed,
            initial_count=initial_count,
            final_count=0,
            desired_count=top_n,
            applied_relax_step=step,
        )
        return sr, so

    id_to_dict = {str(c["id"]): c for c in candidate_dicts}
    batch = [id_to_dict[cid] for cid in first_batch_ids if cid in id_to_dict]

    if direction == "search_job":
        filtered = permission_service.filter_jobs_batch(batch, user_ctx.role)
    else:
        owner_ids = list({r.get("owner_userid", "") for r in batch})
        users_map = _build_users_map(owner_ids, db)
        filtered = permission_service.filter_resumes_batch(batch, users_map, user_ctx.role)
    _set_shadow_served_baseline(
        shadow,
        [str(item.get("id")) for item in filtered],
    )

    shown_ids = [str(r["id"]) for r in batch]
    conversation_service.record_shown(session, shown_ids)

    remaining = conversation_service.get_remaining_count(session)
    if direction == "search_job":
        reason_lines = _build_job_reason_lines_by_id(
            filtered, relaxed, flags, soft_prefs, userid_hash(user_ctx.external_userid),
        )
        reply = _format_job_results(filtered, remaining, reason_lines)
    else:
        reason_lines = _build_resume_reason_lines_by_id(
            filtered, relaxed, flags, soft_prefs, userid_hash(user_ctx.external_userid),
        )
        reply = _format_resume_results(filtered, remaining, reason_lines)
    relaxation_summary = RelaxationSummary(
        field=step,
        label=_relax_step_label(direction, step),
        original_criteria=dict(original_criteria or {}),
        relaxed_criteria=dict(relaxed or {}),
        original_visible_count=original_visible_count,
        relaxed_visible_count=len(filtered),
        relaxed_shown_count=len(filtered),
    )
    reply = f"{_render_relaxation_summary_notice(relaxation_summary)}\n\n{reply}"
    log_event(
        "auto_relax_applied",
        external_userid_hash=userid_hash(user_ctx.external_userid),
        direction=direction,
        field=step,
        original_visible_count=original_visible_count,
        relaxed_visible_count=len(filtered),
        relaxed_shown_count=len(filtered),
        applied=True,
    )

    sr = SearchResult(
        reply_text=reply,
        has_more=remaining > 0,
        result_count=len(filtered),
        recommendation_items=_served_recommendation_items(
            filtered, direction, v1_meta["items"] if v1_meta else None,
        ),
        snapshot_id=(
            v1_meta["snapshot_id"] if v1_meta else
            shadow["snapshot_id"] if shadow else None
        ),
        strategy_assignment=(
            v1_meta["assignment"] if v1_meta else
            shadow["assignment"] if shadow else
            _legacy_fallback_assignment(relaxed_decision)
        ),
        request_id=(
            v1_meta["request_id"] if v1_meta else
            shadow["request_id"] if shadow else None
        ),
        query_digest=digest,
        candidate_ids=(
            v1_meta["candidate_ids"] if v1_meta else
            shadow["candidate_ids"] if shadow else
            [str(item.get("id")) for item in candidate_dicts[:max_candidates]]
        ),
        precision_pool_ids=(
            v1_meta["precision_pool_ids"] if v1_meta else
            shadow["precision_pool_ids"] if shadow else []
        ),
        **_search_attempt_fields(
            v1_meta, rerank_result, scoring_time_utc=request_now_utc,
        ),
    )
    # Phase 5 §5.4：execute_relaxed_search 也统计 soft_pref_hits
    soft_pref_hits = _count_soft_pref_hits(batch, soft_prefs)
    so = _build_search_outcome(
        direction=direction,
        criteria_used=relaxed,
        initial_count=initial_count,
        final_count=len(candidates),
        desired_count=top_n,
        applied_relax_step=step,
        has_more=remaining > 0,
        soft_pref_hits=soft_pref_hits,
        candidate_count_capped=len(candidates),
        visible_count=len(filtered),
        shown_count=len(filtered),
        remaining_count_capped=remaining,
        relaxation_summary=relaxation_summary,
    )
    return sr, so


def _probe_job_suggestions(
    criteria: dict,
    limit: int,
    db: Session,
    *,
    attempt_records: list[dict] | None = None,
) -> list[FallbackSuggestion]:
    """温和放宽全 0 后，探查激进方向给用户做选择（Bug 3）。

    探查步本身不采纳为结果，仅用于"建议方向"文案；保留 has_effective_search_criteria
    守卫，防全表召回。
    """
    probes: list[tuple[str, dict]] = []

    if criteria.get("salary_floor_monthly") is not None:
        c = dict(criteria)
        c.pop("salary_floor_monthly", None)
        c.pop("salary_ceiling_monthly", None)  # 一起去掉，避免上下限"冲突"
        probes.append(("drop_salary", c))

    if criteria.get("job_category"):
        c = dict(criteria)
        c.pop("job_category", None)
        probes.append(("drop_job_category", c))

    cities = criteria.get("city")
    if cities:
        c = {"city": cities}
        # 跳过与原 criteria 等价（无实际放宽）或与已有 probe 重复的方向
        if c != criteria and all(c != prev_c for _, prev_c in probes):
            probes.append(("keep_city_only", c))

    return _collect_suggestions(
        probes, limit, db, _query_jobs, "search_job",
        attempt_records=attempt_records,
    )


def _probe_resume_suggestions(
    criteria: dict,
    limit: int,
    db: Session,
    *,
    attempt_records: list[dict] | None = None,
) -> list[FallbackSuggestion]:
    probes: list[tuple[str, dict]] = []

    if criteria.get("salary_ceiling_monthly") is not None:
        c = dict(criteria)
        c.pop("salary_ceiling_monthly", None)
        probes.append(("drop_salary_ceiling", c))

    if criteria.get("job_category"):
        c = dict(criteria)
        c.pop("job_category", None)
        probes.append(("drop_job_category", c))

    cities = criteria.get("city")
    if cities:
        c = {"city": cities}
        if c != criteria and all(c != prev_c for _, prev_c in probes):
            probes.append(("keep_city_only", c))

    return _collect_suggestions(
        probes, limit, db, _query_resumes, "search_worker",
        attempt_records=attempt_records,
    )


def _collect_suggestions(
    probes: list[tuple[str, dict]],
    limit: int,
    db: Session,
    query_fn,
    direction: str,
    *,
    attempt_records: list[dict] | None = None,
) -> list[FallbackSuggestion]:
    """跑探查并打日志，返回命中数 ≥1 的方向，按命中数降序，截到 _MAX_SUGGESTIONS。"""
    suggestions: list[FallbackSuggestion] = []
    for name, c in probes:
        # 安全护栏：city / job_category 至少一个非空才允许查询
        if not has_effective_search_criteria(c):
            continue
        started = time.perf_counter()
        cands = query_fn(c, limit, db)
        if attempt_records is not None:
            attempt_records.append(_query_attempt_record(
                step=name,
                criteria=c,
                candidates=cands,
                started=started,
            ))
        log_event(
            "search_suggestion_probed",
            direction=direction,
            step=name,
            candidate_count=len(cands),
            criteria_keys=sorted(c.keys()),
        )
        if cands:
            suggestions.append(
                FallbackSuggestion(step=name, criteria=c, count=len(cands)),
            )
    suggestions.sort(key=lambda s: s.count, reverse=True)
    return suggestions[:_MAX_SUGGESTIONS]


def _strip_optional_filters(criteria: dict, optional_keys: tuple[str, ...]) -> dict:
    """返回去掉指定可选硬过滤字段后的新 criteria；保留 city / job_category / 薪资。"""
    stripped = dict(criteria)
    for key in optional_keys:
        stripped.pop(key, None)
    return stripped


def _broaden_job_categories(criteria: dict) -> dict | None:
    """把 job_category 细分类/口语化值映射到 canonical 大类（spec §3.4 Step 3）。

    复用 intent_service 的同义词字典。规整层若已在抽取阶段把 LLM 输出归一到大类，
    本步是 no-op；当 criteria 来自 session.search_criteria 历史值、默认条件兜底
    或未走规整层的旧数据时，才真正起作用。

    Returns:
        新的 criteria dict（job_category 已映射 + 去重）；若无任何变化则返回 None
        以便上层跳过该 fallback 步骤、避免重复查询。
    """
    cats = criteria.get("job_category")
    if not cats:
        return None
    if isinstance(cats, str):
        cats = [cats]
    # 延迟 import 避免与 intent_service 的潜在循环依赖
    from app.services.intent_service import _normalize_job_category_value

    broadened: list[str] = []
    seen: set[str] = set()
    changed = False
    for c in cats:
        canonical = _normalize_job_category_value(c) or c
        if canonical != c:
            changed = True
        if canonical and canonical not in seen:
            seen.add(canonical)
            broadened.append(canonical)
    if not changed:
        return None
    out = dict(criteria)
    out["job_category"] = broadened
    return out


# ---------------------------------------------------------------------------
# ORM → dict 转换
# ---------------------------------------------------------------------------

def _jobs_to_dicts(jobs: list, db: Session) -> list[dict]:
    """将 Job ORM 对象转为字典列表，补充关联用户信息。"""
    if not jobs:
        return []
    owner_ids = list({j.owner_userid for j in jobs})
    users_map = _build_users_map(owner_ids, db)

    result = []
    for j in jobs:
        d = {
            "id": j.id,
            "city": j.city,
            "job_category": j.job_category,
            "salary_floor_monthly": j.salary_floor_monthly,
            "salary_ceiling_monthly": j.salary_ceiling_monthly,
            "pay_type": j.pay_type,
            "headcount": j.headcount,
            "gender_required": j.gender_required,
            "is_long_term": j.is_long_term,
            "district": j.district,
            "provide_meal": j.provide_meal,
            "provide_housing": j.provide_housing,
            "shift_pattern": j.shift_pattern,
            "work_hours": j.work_hours,
            "description": j.description,
            "created_at": str(j.created_at) if j.created_at else "",
            "owner_userid": j.owner_userid,
            # recommendation-v1 inputs: §6.4 quality fields and the §6.3.4 soft
            # preference whitelist.  Omitting them silently caps quality_score
            # and drops preferences the user explicitly stated.
            "employment_type": j.employment_type,
            "accept_couple": j.accept_couple,
            "accept_student": j.accept_student,
            "accept_minority": j.accept_minority,
        }
        user_data = users_map.get(j.owner_userid, {})
        d["company"] = user_data.get("company", "")
        d["contact_person"] = user_data.get("contact_person", "")
        d["phone"] = user_data.get("phone", "")
        result.append(d)
    return result


def _resumes_to_dicts(resumes: list) -> list[dict]:
    """将 Resume ORM 对象转为字典列表。"""
    result = []
    for r in resumes:
        d = {
            "id": r.id,
            "expected_cities": r.expected_cities or [],
            "expected_job_categories": r.expected_job_categories or [],
            "salary_expect_floor_monthly": r.salary_expect_floor_monthly,
            "gender": r.gender,
            "age": r.age,
            "education": r.education,
            "work_experience": r.work_experience,
            "description": r.description,
            "created_at": str(r.created_at) if r.created_at else "",
            "owner_userid": r.owner_userid,
            # recommendation-v1 inputs: §6.4 quality fields plus the §6.9.2
            # similarity dimensions (expected_districts / long-short combo).
            "expected_districts": r.expected_districts or [],
            "available_from": r.available_from,
            "accept_night_shift": r.accept_night_shift,
            "accept_overtime": r.accept_overtime,
            "accept_long_term": r.accept_long_term,
            "accept_short_term": r.accept_short_term,
        }
        result.append(d)
    return result


def _build_users_map(user_ids: list[str], db: Session) -> dict[str, dict]:
    """构建 {userid: user_data} 映射。"""
    if not user_ids:
        return {}
    users = db.query(User).filter(User.external_userid.in_(user_ids)).all()
    return {
        u.external_userid: {
            "display_name": u.display_name,
            "company": u.company,
            "contact_person": u.contact_person,
            "phone": u.phone,
        }
        for u in users
    }


# ---------------------------------------------------------------------------
# 有效性验证
# ---------------------------------------------------------------------------

def _restore_input_order(rows: list, requested_ids: list[str]) -> list:
    """§6.11: `show_more()` must restore database rows into `batch_ids` order.

    `WHERE id IN (...)` returns whatever order the storage engine likes, which
    would silently discard the ranking the snapshot committed to.
    """
    by_id = {str(row.id): row for row in rows}
    return [by_id[cid] for cid in requested_ids if cid in by_id]


def _validate_job_ids(job_ids: list[str], db: Session) -> list:
    """重新查询 ID 列表，过滤已失效的，并按输入顺序恢复。"""
    now = datetime.now(timezone.utc)
    int_ids = [int(i) for i in job_ids if i.isdigit()]
    if not int_ids:
        return []
    rows = db.query(Job).join(User, Job.owner_userid == User.external_userid).filter(
        Job.id.in_(int_ids),
        Job.audit_status == "passed",
        Job.deleted_at.is_(None),
        Job.expires_at > now,
        Job.delist_reason.is_(None),
        User.status == "active",
    ).all()
    return _restore_input_order(rows, job_ids)


def _validate_resume_ids(resume_ids: list[str], db: Session) -> list:
    """重新查询 ID 列表，过滤已失效的，并按输入顺序恢复。"""
    now = datetime.now(timezone.utc)
    int_ids = [int(i) for i in resume_ids if i.isdigit()]
    if not int_ids:
        return []
    rows = db.query(Resume).join(
        User, Resume.owner_userid == User.external_userid,
    ).filter(
        Resume.id.in_(int_ids),
        Resume.audit_status == "passed",
        Resume.deleted_at.is_(None),
        Resume.expires_at > now,
        User.status == "active",
    ).all()
    return _restore_input_order(rows, resume_ids)


# ---------------------------------------------------------------------------
# 格式化
# ---------------------------------------------------------------------------

def _format_job_results(
    jobs: list[dict],
    remaining: int,
    reason_lines_by_id: dict[str, list[str]] | None = None,
) -> str:
    """按 §10.5 格式化岗位结果（工人视角）。"""
    if not jobs:
        return "暂无匹配结果。"

    lines = [f"为您找到 {len(jobs)} 个匹配岗位：\n"]
    markers = ["①", "②", "③", "④", "⑤"]

    for i, j in enumerate(jobs):
        marker = markers[i] if i < len(markers) else f"({i+1})"
        company = j.get("company", "")
        category = j.get("job_category", "")
        title = f"{company} | {category}" if company else category

        salary_floor = j.get("salary_floor_monthly", 0)
        salary_ceil = j.get("salary_ceiling_monthly")
        pay_type = j.get("pay_type", "")
        if salary_ceil and salary_ceil > salary_floor:
            salary_str = f"{salary_floor}-{salary_ceil}元/月"
        else:
            salary_str = f"{salary_floor}元/月"

        benefits = []
        if j.get("provide_meal"):
            benefits.append("包吃")
        if j.get("provide_housing"):
            benefits.append("包住")
        benefit_str = f"（{pay_type}，{''.join(benefits)}）" if benefits else f"（{pay_type}）"

        city = j.get("city", "")
        district = j.get("district", "")
        location = f"{city}{district}" if district else city

        lines.append(f"{marker} {title}")
        lines.append(f"   💰 {salary_str}{benefit_str}")
        lines.append(f"   📍 {location}")
        for reason_line in (reason_lines_by_id or {}).get(str(j.get("id", "")), []):
            lines.append(reason_line)

        shift = j.get("shift_pattern", "")
        hours = j.get("work_hours", "")
        if shift or hours:
            lines.append(f"   🔧 {shift}{'，' + hours if hours else ''}")
        lines.append("")

    if remaining > 0:
        lines.append(f'还有 {remaining} 个相关岗位，回复"更多"继续查看')
    lines.append('不满意？直接告诉我调整方向，比如"薪资再高点""要包住的"')

    return "\n".join(lines)


def _format_resume_results(
    resumes: list[dict],
    remaining: int,
    reason_lines_by_id: dict[str, list[str]] | None = None,
) -> str:
    """按 §10.5 格式化简历结果（厂家/中介视角）。"""
    if not resumes:
        return "暂无匹配结果。"

    lines = [f"为您找到 {len(resumes)} 位匹配的求职者：\n"]
    markers = ["①", "②", "③", "④", "⑤"]

    for i, r in enumerate(resumes):
        marker = markers[i] if i < len(markers) else f"({i+1})"
        name = r.get("display_name", "求职者")
        gender = r.get("gender", "")
        age = r.get("age", "")
        title = f"{name} | {gender} {age}岁" if gender and age else name

        categories = r.get("expected_job_categories", [])
        cat_str = "/".join(categories) if categories else ""
        salary = r.get("salary_expect_floor_monthly", 0)

        cities = r.get("expected_cities", [])
        city_str = "、".join(cities) if cities else ""

        lines.append(f"{marker} {title}")
        if cat_str or salary:
            lines.append(f"   🔧 期望：{cat_str}，{salary}+/月")
        if city_str:
            lines.append(f"   📍 期望城市：{city_str}")
        for reason_line in (reason_lines_by_id or {}).get(str(r.get("id", "")), []):
            lines.append(reason_line)

        phone = r.get("phone")
        placeholder = r.get("phone_placeholder")
        if phone:
            lines.append(f"   📞 联系电话：{phone}")
        elif placeholder:
            lines.append(f"   📞 {placeholder}")

        exp = r.get("work_experience", "")
        if exp:
            lines.append(f"   💼 经验：{exp[:50]}")
        lines.append("")

    if remaining > 0:
        lines.append(f'还有 {remaining} 位相关求职者，回复"更多"继续查看')

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Bug 3：0 命中 + 有 suggestions 时的文案
# ---------------------------------------------------------------------------

def _summarize_search_criteria(criteria: dict, salary_key: str) -> str:
    """把原 criteria 渲染为简短摘要（"北京·餐饮·≥2200"），供"原条件下没有匹配"前置语用。"""
    parts: list[str] = []
    cities = criteria.get("city") or []
    if isinstance(cities, str):
        cities = [cities]
    if cities:
        parts.append("/".join(str(c) for c in cities[:2]))
    cats = criteria.get("job_category") or []
    if isinstance(cats, str):
        cats = [cats]
    if cats:
        parts.append("/".join(str(c) for c in cats[:2]))
    salary = criteria.get(salary_key)
    if salary is not None:
        prefix = "≥" if salary_key == "salary_floor_monthly" else "≤"
        parts.append(f"{prefix}{salary}")
    return "·".join(parts) if parts else "当前条件"


def _format_no_match_with_suggestions_job(
    criteria: dict,
    suggestions: list[FallbackSuggestion],
) -> str:
    summary = _summarize_search_criteria(criteria, "salary_floor_monthly")
    lines = [
        f"原条件（{summary}）下没有匹配的岗位。",
        "可以放宽以下方向（已确认有结果）：",
    ]
    for i, s in enumerate(suggestions, 1):
        label = _SUGGESTION_LABEL_JOB.get(s.step, s.step)
        lines.append(f"{i}. {label} —— 约 {s.count} 条")
    lines.append('告诉我您想换哪种条件，比如"不限薪资重新搜"。')
    return "\n".join(lines)


def _format_no_match_with_suggestions_resume(
    criteria: dict,
    suggestions: list[FallbackSuggestion],
) -> str:
    summary = _summarize_search_criteria(criteria, "salary_ceiling_monthly")
    lines = [
        f"原条件（{summary}）下没有匹配的求职者。",
        "可以放宽以下方向（已确认有结果）：",
    ]
    for i, s in enumerate(suggestions, 1):
        label = _SUGGESTION_LABEL_RESUME.get(s.step, s.step)
        lines.append(f"{i}. {label} —— 约 {s.count} 位")
    lines.append('告诉我您想换哪种条件，比如"不限期望薪资重新搜"。')
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _is_job_search(session: SessionState, user_ctx: UserContext) -> bool:
    """判断当前搜索方向。"""
    if user_ctx.role == "worker":
        return True
    if user_ctx.role == "broker" and session.broker_direction:
        return session.broker_direction == "search_job"
    # factory 默认找工人
    return False


def _get_config_int(key: str, db: Session, default: int) -> int:
    """从 system_config 读取整数配置。"""
    config = db.query(SystemConfig).filter(
        SystemConfig.config_key == key,
    ).first()
    if config:
        try:
            return int(config.config_value)
        except (ValueError, TypeError):
            pass
    return default


def _get_config_bool(key: str, db: Session, default: bool) -> bool:
    """从 system_config 读取布尔配置。"""
    config = db.query(SystemConfig).filter(
        SystemConfig.config_key == key,
    ).first()
    if config:
        val = config.config_value.strip().lower()
        return val in ("true", "1", "yes")
    return default
