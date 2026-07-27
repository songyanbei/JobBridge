"""推荐报表指标查询层（§11.9）。

本模块是 §11.9 "指标真源" 表格的唯一实现入口，API 层只做参数校验和序列化：

| 指标 | 真源 |
|---|---|
| 最终请求、业务零结果、owner 分布 | ``recommendation_request`` |
| 查询 attempt、LLM 回退、排序延迟、token、召回池成员 | ``recommendation_search_attempt`` |
| 实际曝光、重复率、集中度、探索曝光 | ``recommendation_impression`` |
| 策略 CTR、探索 CTR | 带 delivery 归因的 ``event_log`` ÷ impression |
| 投递可靠性 | ``recommendation_delivery`` |
| 自然日曝光报表 | ``recommendation_exposure_daily`` |

三条硬性口径：

* 时间一律走 ``app.core.time_utils``。窗口是滚动 UTC 小时窗（§9.12"最近 7 日曝光
  使用滚动 168 小时 UTC"），只有自然日报表才用 ``stat_date``；
* "召回池曝光集中度" 的分母是 **全召回池**——只展开
  ``recommendation_request.served_attempt_id`` 指向的那条 attempt 的
  ``candidate_ids``，零曝光候选补 0。只统计已曝光候选的那个版本必须叫
  ``exposed_candidate_gini``，两者不得混用（§11.9）；
* 策略 CTR / 探索 CTR 的分子只取 ``attribution_status='attributed'`` 且能在
  ``recommendation_impression`` 里找到对应事实的点击，按
  ``(delivery_id, target_type, target_id)`` 去重，保证分子恒为分母子集（§9.9）。

输出全部是聚合量，不含 ``viewer_userid``（§9.11）。``viewer_userid`` 只在进程内
用于"相同条件 Top 3 重复率"的分组，不会出现在返回值里。
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Sequence

from sqlalchemy import and_, case, func
from sqlalchemy.orm import Session

from app.core.time_utils import business_timezone, ensure_utc, to_naive_utc, utc_now
from app.models import (
    EventLog,
    RecommendationDelivery,
    RecommendationExposureDaily,
    RecommendationImpression,
    RecommendationRequest,
    RecommendationSearchAttempt,
)
from app.schemas.report import (
    AttemptMetrics,
    ClickMetrics,
    DeliveryMetrics,
    ExposureDailyPoint,
    ExposureDailyResponse,
    ExposureMetrics,
    LlmCostMetrics,
    MetricsWindow,
    RecallPoolMetrics,
    RecommendationMetricsResponse,
    RequestMetrics,
    ShadowMetrics,
    StrategyCtrPoint,
)

MAX_WINDOW_DAYS = 90

# 明细扫描上限。P95/P99 与 Top 3 重复率没有可用的 MySQL 8 分位函数，只能取回
# 明细在内存里算；超过上限时截断最近的一段并置 ``truncated`` 标志，宁可少算也
# 不让报表拖垮库。
SCAN_LIMIT = 50_000
RECALL_POOL_ATTEMPT_LIMIT = 5_000
_ID_CHUNK = 500

# §10.5 的状态机口径。实现里还残留 ``sending``/``redacted``（TTL 脱敏后的 sent），
# 一并列出以免报表把它们算成"未知状态"。
DELIVERY_STATUSES = (
    "prepared", "pending", "sending", "sent",
    "retry_wait", "permanent_failed", "unknown",
)
SENT_STATUSES = ("sent", "redacted")
IMPRESSION_STATES = ("pending", "processing", "completed", "retry", "failed")
IMPRESSION_BACKLOG_STATES = ("pending", "processing", "retry", "deriving")

# probe / shadow attempt 不进入召回池分母（§11.9、§14.5）。
NON_SERVING_ATTEMPT_KINDS = ("relax_probe", "shadow_candidate")

# persistence executor 队列满时不能再同步写数据库，否则会把本应非阻塞的 shadow
# 反压到服务链路；该事件目前只存在于结构化日志，需由日志指标系统聚合。
SHADOW_MISSING_SOURCES = (
    "structured_log.event='shadow_persistence_dropped'",
)


# ---------------------------------------------------------------------------
# 纯计算工具
# ---------------------------------------------------------------------------

def _ratio(numerator: float, denominator: float) -> float:
    """安全比率；分母为 0 时返回 0.0 而不是抛除零。"""
    if not denominator:
        return 0.0
    return round(float(numerator) / float(denominator), 6)


def gini(values: Iterable[int]) -> float:
    """基尼系数。0=完全均匀，1=完全集中。

    调用方必须先把零曝光候选补成 0 再传进来——把 0 排除掉算出的是另一个指标
    （已曝光候选集中度），两者不可互换（§11.9）。
    """
    data = sorted(int(value) for value in values)
    n = len(data)
    total = sum(data)
    if n == 0 or total <= 0:
        return 0.0
    weighted = sum((index + 1) * value for index, value in enumerate(data))
    return round((2 * weighted) / (n * total) - (n + 1) / n, 6)


def percentile(values: Sequence[float], pct: float) -> float | None:
    """线性插值分位数；空样本返回 None（"没有样本"≠"0 毫秒"）。"""
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    rank = (len(ordered) - 1) * pct
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return round(ordered[low], 3)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (rank - low), 3)


def target_type_for_direction(direction: str) -> str:
    """方向 → 曝光目标类型，与 ``recommendation_request_service`` 的写入口径一致。"""
    return "job" if direction == "search_job" else "resume"


def _shares(counts: dict[str, int]) -> dict[str, float]:
    total = sum(counts.values())
    return {key: _ratio(value, total) for key, value in counts.items()}


def _chunked(items: Sequence[Any], size: int = _ID_CHUNK) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


# ---------------------------------------------------------------------------
# 窗口
# ---------------------------------------------------------------------------

def resolve_window(days: int, now: datetime | None = None) -> tuple[datetime, datetime]:
    """滚动窗口的 naive UTC 边界，直接可与 DATETIME 列比较（§9.12）。

    刻意不用自然日：报表窗口是滚动 ``days*24`` 小时 UTC，只有
    ``recommendation_exposure_daily`` 才走 Asia/Shanghai 自然日。
    """
    end_aware = ensure_utc(now) or utc_now()
    end = to_naive_utc(end_aware)
    return end - timedelta(days=int(days)), end


# ---------------------------------------------------------------------------
# 请求级指标（真源 recommendation_request）
# ---------------------------------------------------------------------------

def _request_base(db: Session, direction: str | None, start: datetime, end: datetime):
    query = db.query(RecommendationRequest).filter(
        RecommendationRequest.created_at >= start,
        RecommendationRequest.created_at < end,
    )
    if direction:
        query = query.filter(RecommendationRequest.direction == direction)
    return query


def _request_section(
    db: Session, direction: str | None, start: datetime, end: datetime,
) -> RequestMetrics:
    base = _request_base(db, direction, start, end)

    kind_counts = {
        str(kind): int(count)
        for kind, count in base.with_entities(
            RecommendationRequest.request_kind, func.count(RecommendationRequest.request_id),
        ).group_by(RecommendationRequest.request_kind).all()
    }
    mode_counts = {
        str(mode): int(count)
        for mode, count in base.with_entities(
            RecommendationRequest.execution_mode, func.count(RecommendationRequest.request_id),
        ).group_by(RecommendationRequest.execution_mode).all()
    }
    assignment_counts = {
        str(assignment): int(count)
        for assignment, count in base.with_entities(
            RecommendationRequest.served_assignment, func.count(RecommendationRequest.request_id),
        ).group_by(RecommendationRequest.served_assignment).all()
    }
    for mode in ("off", "shadow", "on"):
        mode_counts.setdefault(mode, 0)
    for assignment in ("legacy", "stable", "candidate"):
        assignment_counts.setdefault(assignment, 0)

    totals = base.with_entities(
        func.count(RecommendationRequest.request_id),
        func.coalesce(func.sum(case((RecommendationRequest.is_zero_result == 1, 1), else_=0)), 0),
        func.coalesce(func.sum(case((RecommendationRequest.show_more_exhausted == 1, 1), else_=0)), 0),
        func.coalesce(func.sum(
            case((and_(RecommendationRequest.is_zero_result == 1,
                       RecommendationRequest.show_more_exhausted == 0), 1), else_=0),
        ), 0),
    ).one()
    total, zero_result, show_more_exhausted, business_zero = (int(value) for value in totals)

    # §14.5：show_more 耗尽只记 show_more_exhausted，不计入业务零结果率，
    # 因此分母也要把这些请求剔除，否则翻页越多零结果率越"好看"。
    zero_denominator = total - show_more_exhausted

    scanned = base.with_entities(
        RecommendationRequest.viewer_userid,
        RecommendationRequest.direction,
        RecommendationRequest.query_digest,
        RecommendationRequest.created_at,
        RecommendationRequest.served_top_ids,
        RecommendationRequest.result_count,
        RecommendationRequest.served_max_owner_items,
        RecommendationRequest.total_latency_ms,
    ).order_by(RecommendationRequest.created_at.desc()).limit(SCAN_LIMIT + 1).all()
    truncated = len(scanned) > SCAN_LIMIT
    scanned = scanned[:SCAN_LIMIT]

    # 相同条件 = 同 viewer + 同方向 + 同 query_digest。viewer_userid 只在这里做
    # 内存分组，绝不写进返回值（§9.11）。
    groups: dict[tuple[str, str, str], list[tuple[datetime, frozenset[str]]]] = defaultdict(list)
    single_owner_top3 = 0
    top3_eligible = 0
    latencies: list[float] = []
    for viewer, req_direction, digest, created_at, top_ids, result_count, max_owner_items, latency in scanned:
        ids = frozenset(str(item) for item in (top_ids or []))
        groups[(viewer, str(req_direction), str(digest))].append((created_at, ids))
        if int(result_count or 0) >= 3:
            top3_eligible += 1
            if int(max_owner_items or 0) >= 3:
                single_owner_top3 += 1
        if latency:
            latencies.append(float(latency))

    repeat_pairs = 0
    repeat_hits = 0
    for entries in groups.values():
        if len(entries) < 2:
            continue
        entries.sort(key=lambda item: item[0])
        for previous, current in zip(entries, entries[1:]):
            if not previous[1] or not current[1]:
                continue
            repeat_pairs += 1
            if previous[1] == current[1]:
                repeat_hits += 1

    return RequestMetrics(
        total=total,
        zero_result=zero_result,
        business_zero_result=business_zero,
        zero_result_rate=_ratio(business_zero, zero_denominator),
        show_more_exhausted=show_more_exhausted,
        by_kind=kind_counts,
        execution_mode_counts=mode_counts,
        execution_mode_share=_shares(mode_counts),
        assignment_counts=assignment_counts,
        assignment_share=_shares(assignment_counts),
        top3_single_owner_rate=_ratio(single_owner_top3, top3_eligible),
        top3_single_owner_requests=single_owner_top3,
        top3_repeat_rate=_ratio(repeat_hits, repeat_pairs),
        top3_repeat_pairs=repeat_pairs,
        total_latency_p95_ms=percentile(latencies, 0.95),
        total_latency_p99_ms=percentile(latencies, 0.99),
        truncated=truncated,
    )


# ---------------------------------------------------------------------------
# attempt 级指标（真源 recommendation_search_attempt）
# ---------------------------------------------------------------------------

def _attempt_base(db: Session, direction: str | None, start: datetime, end: datetime):
    query = db.query(RecommendationSearchAttempt).filter(
        RecommendationSearchAttempt.created_at >= start,
        RecommendationSearchAttempt.created_at < end,
    )
    if direction:
        query = query.join(
            RecommendationRequest,
            RecommendationRequest.request_id == RecommendationSearchAttempt.request_id,
        ).filter(RecommendationRequest.direction == direction)
    return query


def _attempt_section(
    db: Session, direction: str | None, start: datetime, end: datetime,
) -> AttemptMetrics:
    # shadow 有独立的差异、容量与成本分区；混进服务 attempt 会让候选策略超时
    # 污染用户实际经历的 reranker 回退率和延迟。
    base = _attempt_base(db, direction, start, end).filter(
        RecommendationSearchAttempt.attempt_kind != "shadow_candidate",
    )
    total = int(base.with_entities(
        func.count(RecommendationSearchAttempt.attempt_id),
    ).scalar() or 0)
    ranking_base = base.filter(
        RecommendationSearchAttempt.attempt_kind != "relax_probe",
    )
    totals = ranking_base.with_entities(
        func.count(RecommendationSearchAttempt.attempt_id),
        func.coalesce(func.sum(
            case((RecommendationSearchAttempt.ranking_fallback.isnot(None), 1), else_=0),
        ), 0),
        func.coalesce(func.sum(
            case((RecommendationSearchAttempt.is_zero_result == 1, 1), else_=0),
        ), 0),
        func.coalesce(func.sum(RecommendationSearchAttempt.llm_retry_count), 0),
    ).one()
    ranking_attempts, fallback, _rank_zero_candidate, retries = (
        int(value) for value in totals
    )
    zero_candidate = int(base.with_entities(
        func.coalesce(func.sum(
            case((RecommendationSearchAttempt.is_zero_result == 1, 1), else_=0),
        ), 0),
    ).scalar() or 0)

    fallback_reasons = {
        str(reason): int(count)
        for reason, count in ranking_base.with_entities(
            RecommendationSearchAttempt.ranking_fallback,
            func.count(RecommendationSearchAttempt.attempt_id),
        ).filter(
            RecommendationSearchAttempt.ranking_fallback.isnot(None),
        ).group_by(RecommendationSearchAttempt.ranking_fallback).all()
    }
    llm_status_counts = {
        str(status): int(count)
        for status, count in ranking_base.with_entities(
            RecommendationSearchAttempt.llm_status,
            func.count(RecommendationSearchAttempt.attempt_id),
        ).group_by(RecommendationSearchAttempt.llm_status).all()
    }

    latencies = [
        float(value or 0)
        for (value,) in ranking_base.with_entities(
            RecommendationSearchAttempt.ranking_latency_ms,
        ).order_by(RecommendationSearchAttempt.created_at.desc()).limit(SCAN_LIMIT).all()
    ]

    return AttemptMetrics(
        total=total,
        ranking_attempts=ranking_attempts,
        reranker_fallback=fallback,
        reranker_fallback_rate=_ratio(fallback, ranking_attempts),
        fallback_by_reason=fallback_reasons,
        llm_status_counts=llm_status_counts,
        llm_status_share=_shares(llm_status_counts),
        llm_retry_count=retries,
        zero_candidate_attempts=zero_candidate,
        zero_candidate_rate=_ratio(zero_candidate, total),
        ranking_latency_p95_ms=percentile(latencies, 0.95),
        ranking_latency_p99_ms=percentile(latencies, 0.99),
    )


# ---------------------------------------------------------------------------
# 曝光指标（真源 recommendation_impression）
# ---------------------------------------------------------------------------

def _impression_base(db: Session, direction: str | None, start: datetime, end: datetime):
    query = db.query(RecommendationImpression).filter(
        RecommendationImpression.exposed_at >= start,
        RecommendationImpression.exposed_at < end,
    )
    if direction:
        query = query.filter(RecommendationImpression.direction == direction)
    return query


def _impression_counts_by_target(
    db: Session, direction: str | None, start: datetime, end: datetime,
) -> dict[tuple[str, int], int]:
    rows = _impression_base(db, direction, start, end).with_entities(
        RecommendationImpression.target_type,
        RecommendationImpression.target_id,
        func.count(RecommendationImpression.id),
    ).group_by(
        RecommendationImpression.target_type,
        RecommendationImpression.target_id,
    ).all()
    return {(str(target_type), int(target_id)): int(count) for target_type, target_id, count in rows}


def _exposure_section(
    db: Session,
    direction: str | None,
    start: datetime,
    end: datetime,
    target_counts: dict[tuple[str, int], int],
) -> ExposureMetrics:
    base = _impression_base(db, direction, start, end)
    totals = base.with_entities(
        func.count(RecommendationImpression.id),
        func.coalesce(func.sum(
            case((RecommendationImpression.is_exploration == 1, 1), else_=0),
        ), 0),
        func.count(func.distinct(RecommendationImpression.viewer_userid)),
    ).one()
    impressions, exploration, exposed_users = (int(value) for value in totals)

    return ExposureMetrics(
        impressions=impressions,
        exposed_users=exposed_users,
        exposed_candidates=len(target_counts),
        exploration_impressions=exploration,
        exploration_share=_ratio(exploration, impressions),
        # §11.9：只对出现过曝光的候选算出来的集中度必须单独命名，不得与召回池
        # 集中度混用。
        exposed_candidate_gini=gini(target_counts.values()),
    )


# ---------------------------------------------------------------------------
# 召回池曝光集中度（真源 request.served_attempt_id → attempt.candidate_ids）
# ---------------------------------------------------------------------------

def _recall_pool_section(
    db: Session,
    direction: str | None,
    start: datetime,
    end: datetime,
    target_counts: dict[tuple[str, int], int],
) -> RecallPoolMetrics:
    """展开统计窗口内被真正采用的 attempt，零曝光候选补 0 后算 Gini。

    ``served_attempt_id`` 取 DISTINCT，因此 show_more 复用同一条 served attempt
    时不会重复放大分母；probe / shadow attempt 本来就不会被 request 引用，这里
    再按 ``attempt_kind`` 兜一层，防止上游写入口径变化后污染分母（§11.9）。
    """
    rows = _request_base(db, direction, start, end).with_entities(
        RecommendationRequest.served_attempt_id,
        RecommendationRequest.direction,
    ).filter(
        RecommendationRequest.served_attempt_id.isnot(None),
    ).distinct().order_by(
        RecommendationRequest.served_attempt_id,
    ).limit(RECALL_POOL_ATTEMPT_LIMIT + 1).all()
    truncated = len(rows) > RECALL_POOL_ATTEMPT_LIMIT
    rows = rows[:RECALL_POOL_ATTEMPT_LIMIT]

    attempt_direction = {str(attempt_id): str(req_direction) for attempt_id, req_direction in rows}
    attempt_ids = list(attempt_direction)

    pool: set[tuple[str, int]] = set()
    for chunk in _chunked(attempt_ids):
        candidate_rows = db.query(
            RecommendationSearchAttempt.attempt_id,
            RecommendationSearchAttempt.candidate_ids,
        ).filter(
            RecommendationSearchAttempt.attempt_id.in_(list(chunk)),
            RecommendationSearchAttempt.attempt_kind.notin_(NON_SERVING_ATTEMPT_KINDS),
        ).all()
        for attempt_id, candidate_ids in candidate_rows:
            target_type = target_type_for_direction(attempt_direction.get(str(attempt_id), ""))
            for candidate_id in candidate_ids or []:
                try:
                    pool.add((target_type, int(candidate_id)))
                except (TypeError, ValueError):
                    continue

    counts = [target_counts.get(member, 0) for member in pool]
    exposed = sum(1 for value in counts if value > 0)
    return RecallPoolMetrics(
        attempts=len(attempt_ids),
        pool_candidates=len(pool),
        exposed_candidates=exposed,
        coverage=_ratio(exposed, len(pool)),
        gini=gini(counts),
        truncated=truncated,
    )


# ---------------------------------------------------------------------------
# 点击与 CTR（真源 event_log ÷ impression）
# ---------------------------------------------------------------------------

def _attributed_click_join(db: Session, direction: str | None, start: datetime, end: datetime):
    """attributed 点击 ⋈ 曝光事实。

    join 到 impression 而不是 delivery，保证 CTR 分子恒为分母的子集：没有曝光
    事实的点击（delivery 尚未 sent、或曝光派生失败）不会先于分母被计入（§9.9）。
    按 impression.id join 天然实现了 ``(delivery_id, target_type, target_id)`` 去重。
    """
    return _impression_base(db, direction, start, end).join(
        EventLog,
        and_(
            EventLog.delivery_id == RecommendationImpression.delivery_id,
            EventLog.target_type == RecommendationImpression.target_type,
            EventLog.target_id == RecommendationImpression.target_id,
        ),
    ).filter(EventLog.attribution_status == "attributed")


def _click_section(
    db: Session, direction: str | None, start: datetime, end: datetime, impressions: int,
) -> ClickMetrics:
    clicked = int(
        _attributed_click_join(db, direction, start, end).with_entities(
            func.count(func.distinct(RecommendationImpression.id)),
        ).scalar() or 0
    )

    clicks_by_exploration = {
        bool(flag): int(count)
        for flag, count in _attributed_click_join(db, direction, start, end).with_entities(
            RecommendationImpression.is_exploration,
            func.count(func.distinct(RecommendationImpression.id)),
        ).group_by(RecommendationImpression.is_exploration).all()
    }
    impressions_by_exploration = {
        bool(flag): int(count)
        for flag, count in _impression_base(db, direction, start, end).with_entities(
            RecommendationImpression.is_exploration,
            func.count(RecommendationImpression.id),
        ).group_by(RecommendationImpression.is_exploration).all()
    }

    version_impressions = {
        (int(version) if version is not None else None): int(count)
        for version, count in _impression_base(db, direction, start, end).with_entities(
            RecommendationImpression.strategy_version_id,
            func.count(RecommendationImpression.id),
        ).group_by(RecommendationImpression.strategy_version_id).all()
    }
    version_clicks = {
        (int(version) if version is not None else None): int(count)
        for version, count in _attributed_click_join(db, direction, start, end).with_entities(
            RecommendationImpression.strategy_version_id,
            func.count(func.distinct(RecommendationImpression.id)),
        ).group_by(RecommendationImpression.strategy_version_id).all()
    }
    by_version = [
        StrategyCtrPoint(
            strategy_version_id=version,
            impressions=count,
            clicks=version_clicks.get(version, 0),
            ctr=_ratio(version_clicks.get(version, 0), count),
        )
        for version, count in sorted(
            version_impressions.items(), key=lambda item: (item[0] is not None, item[0]),
        )
    ]

    # 归因比例覆盖窗口内全部点击。legacy 无 delivery_id 的点击无法判定方向，
    # 因此这一块刻意不跟随 direction 过滤，由 direction_scoped=False 显式说明。
    status_counts = {
        str(status): int(count)
        for status, count in db.query(
            EventLog.attribution_status, func.count(EventLog.id),
        ).filter(
            EventLog.event_type == "miniprogram_click",
            EventLog.occurred_at >= start,
            EventLog.occurred_at < end,
        ).group_by(EventLog.attribution_status).all()
    }
    for status in ("attributed", "legacy_unattributed", "rejected"):
        status_counts.setdefault(status, 0)
    status_total = sum(status_counts.values())

    return ClickMetrics(
        attributed_impression_clicks=clicked,
        ctr=_ratio(clicked, impressions),
        exploration_clicks=clicks_by_exploration.get(True, 0),
        exploration_ctr=_ratio(
            clicks_by_exploration.get(True, 0), impressions_by_exploration.get(True, 0),
        ),
        non_exploration_ctr=_ratio(
            clicks_by_exploration.get(False, 0), impressions_by_exploration.get(False, 0),
        ),
        by_strategy_version=by_version,
        attribution_counts=status_counts,
        attribution_share=_shares(status_counts),
        attributed_click_rate=_ratio(status_counts.get("attributed", 0), status_total),
        attribution_direction_scoped=False,
    )


# ---------------------------------------------------------------------------
# 投递可靠性（真源 recommendation_delivery）
# ---------------------------------------------------------------------------

def _delivery_base(db: Session, direction: str | None, start: datetime, end: datetime):
    query = db.query(RecommendationDelivery).filter(
        RecommendationDelivery.created_at >= start,
        RecommendationDelivery.created_at < end,
    )
    if direction:
        query = query.join(
            RecommendationRequest,
            RecommendationRequest.request_id == RecommendationDelivery.request_id,
        ).filter(RecommendationRequest.direction == direction)
    return query


def _delivery_section(
    db: Session, direction: str | None, start: datetime, end: datetime, now: datetime,
) -> DeliveryMetrics:
    base = _delivery_base(db, direction, start, end)

    status_counts = {status: 0 for status in DELIVERY_STATUSES}
    for status, count in base.with_entities(
        RecommendationDelivery.status, func.count(RecommendationDelivery.delivery_id),
    ).group_by(RecommendationDelivery.status).all():
        status_counts[str(status)] = status_counts.get(str(status), 0) + int(count)
    total = sum(status_counts.values())

    impression_state_counts = {state: 0 for state in IMPRESSION_STATES}
    for state, count in base.with_entities(
        RecommendationDelivery.impression_state, func.count(RecommendationDelivery.delivery_id),
    ).filter(
        RecommendationDelivery.status.in_(SENT_STATUSES),
    ).group_by(RecommendationDelivery.impression_state).all():
        impression_state_counts[str(state)] = impression_state_counts.get(str(state), 0) + int(count)
    sent_total = sum(impression_state_counts.values())
    backlog = sum(impression_state_counts.get(state, 0) for state in IMPRESSION_BACKLOG_STATES)

    # 状态 age：MySQL 8 没有分位函数，取回明细在内存算；按 created_at 倒序截断，
    # 保证截断掉的是最老的一段而不是随机行。
    age_rows = base.with_entities(
        RecommendationDelivery.status, RecommendationDelivery.created_at,
    ).order_by(RecommendationDelivery.created_at.desc()).limit(SCAN_LIMIT + 1).all()
    truncated = len(age_rows) > SCAN_LIMIT
    ages: dict[str, list[float]] = defaultdict(list)
    for status, created_at in age_rows[:SCAN_LIMIT]:
        if created_at is None:
            continue
        ages[str(status)].append(max(0.0, (now - created_at).total_seconds()))
    status_age = {
        status: {"p95": percentile(values, 0.95), "p99": percentile(values, 0.99)}
        for status, values in ages.items()
    }

    latency_rows = base.with_entities(
        RecommendationDelivery.sent_at, RecommendationDelivery.impression_derived_at,
    ).filter(
        RecommendationDelivery.impression_state == "completed",
        RecommendationDelivery.sent_at.isnot(None),
        RecommendationDelivery.impression_derived_at.isnot(None),
    ).order_by(RecommendationDelivery.created_at.desc()).limit(SCAN_LIMIT).all()
    derive_latencies = [
        max(0.0, (derived_at - sent_at).total_seconds() * 1000.0)
        for sent_at, derived_at in latency_rows
    ]

    return DeliveryMetrics(
        total=total,
        status_counts=status_counts,
        status_share=_shares(status_counts),
        unknown_rate=_ratio(status_counts.get("unknown", 0), total),
        status_age_seconds=status_age,
        impression_state_counts=impression_state_counts,
        impression_backlog=backlog,
        impression_backlog_rate=_ratio(backlog, sent_total),
        sent_to_impression_p95_ms=percentile(derive_latencies, 0.95),
        sent_to_impression_p99_ms=percentile(derive_latencies, 0.99),
        sent_to_impression_samples=len(derive_latencies),
        # 下面两项没有事实来源：prepared session CAS 冲突与 dispatcher claim
        # 延迟目前都只打日志、不落库。返回 None 而不是 0，避免报表把"没埋点"
        # 读成"没有冲突"。
        prepared_session_conflicts=None,
        dispatcher_claim_latency_p95_ms=None,
        truncated=truncated,
    )


# ---------------------------------------------------------------------------
# LLM 成本 / shadow
# ---------------------------------------------------------------------------

def _llm_section(
    db: Session, direction: str | None, start: datetime, end: datetime,
) -> LlmCostMetrics:
    rows = db.query(
        RecommendationRequest.direction,
        RecommendationSearchAttempt.attempt_kind,
        func.coalesce(func.sum(RecommendationSearchAttempt.llm_input_tokens), 0),
        func.coalesce(func.sum(RecommendationSearchAttempt.llm_output_tokens), 0),
        func.count(RecommendationSearchAttempt.attempt_id),
    ).join(
        RecommendationRequest,
        RecommendationRequest.request_id == RecommendationSearchAttempt.request_id,
    ).filter(
        RecommendationSearchAttempt.created_at >= start,
        RecommendationSearchAttempt.created_at < end,
    )
    if direction:
        rows = rows.filter(RecommendationRequest.direction == direction)
    grouped_rows = rows.group_by(
        RecommendationRequest.direction,
        RecommendationSearchAttempt.attempt_kind,
    ).all()
    by_direction: dict[str, dict[str, int]] = {}
    legacy_input_tokens = 0
    legacy_output_tokens = 0
    shadow_input_tokens = 0
    shadow_output_tokens = 0
    for row_direction, attempt_kind, input_tokens, output_tokens, attempts in grouped_rows:
        key = str(row_direction)
        bucket = by_direction.setdefault(key, {
            "input_tokens": 0,
            "output_tokens": 0,
            "attempts": 0,
            "legacy_input_tokens": 0,
            "legacy_output_tokens": 0,
            "legacy_attempts": 0,
            "shadow_input_tokens": 0,
            "shadow_output_tokens": 0,
            "shadow_attempts": 0,
        })
        input_count = int(input_tokens)
        output_count = int(output_tokens)
        attempt_count = int(attempts)
        bucket["input_tokens"] += input_count
        bucket["output_tokens"] += output_count
        bucket["attempts"] += attempt_count
        if str(attempt_kind) == "shadow_candidate":
            bucket["shadow_input_tokens"] += input_count
            bucket["shadow_output_tokens"] += output_count
            bucket["shadow_attempts"] += attempt_count
            shadow_input_tokens += input_count
            shadow_output_tokens += output_count
        else:
            bucket["legacy_input_tokens"] += input_count
            bucket["legacy_output_tokens"] += output_count
            bucket["legacy_attempts"] += attempt_count
            legacy_input_tokens += input_count
            legacy_output_tokens += output_count
    return LlmCostMetrics(
        legacy_input_tokens=legacy_input_tokens,
        legacy_output_tokens=legacy_output_tokens,
        by_direction=by_direction,
        shadow_input_tokens=shadow_input_tokens,
        shadow_output_tokens=shadow_output_tokens,
        # 单价表与 provider 限流事实都还没有落库，返回 None 表示"无数据源"。
        daily_cost_by_direction=None,
        provider_throttle_rate=None,
    )


def _shadow_section(
    db: Session, direction: str | None, start: datetime, end: datetime,
) -> ShadowMetrics:
    rows = _request_base(db, direction, start, end).filter(
        RecommendationRequest.shadow_status.isnot(None),
    ).with_entities(
        RecommendationRequest.shadow_status,
        RecommendationRequest.shadow_fallback,
        RecommendationRequest.served_top_ids,
        RecommendationRequest.shadow_top_ids,
        RecommendationRequest.shadow_overlap_count,
        RecommendationRequest.shadow_rank_delta,
        RecommendationRequest.shadow_queue_wait_ms,
        RecommendationRequest.shadow_latency_ms,
    ).order_by(
        RecommendationRequest.created_at.desc(),
    ).limit(SCAN_LIMIT).all()

    overlap_count = 0
    overlap_denominator = 0
    position_deltas: list[float] = []
    queue_waits: list[float] = []
    durations: list[float] = []
    timeout_count = 0
    local_capacity_skip_count = 0
    global_capacity_skip_count = 0

    for (
        status,
        fallback,
        served_top_ids,
        shadow_top_ids,
        stored_overlap,
        rank_delta,
        queue_wait_ms,
        latency_ms,
    ) in rows:
        status_text = str(status)
        fallback_text = str(fallback or "")
        if status_text in ("timeout", "timeout_in_queue"):
            timeout_count += 1
        if status_text == "skipped_capacity" and fallback_text == "local_capacity":
            local_capacity_skip_count += 1
        if status_text == "skipped_capacity" and fallback_text == "global_capacity":
            global_capacity_skip_count += 1

        served_ids = [str(item) for item in (served_top_ids or [])]
        shadow_ids = [str(item) for item in (shadow_top_ids or [])]
        if status_text == "completed" and (served_ids or shadow_ids):
            # Top-N overlap 的分母取双侧实际 Top 列表较长者；候选策略少返回条目时
            # 不能因为缩小分母而虚高。stored_overlap 是写入时计算的集合交集。
            overlap_denominator += max(len(served_ids), len(shadow_ids))
            overlap_count += int(
                stored_overlap
                if stored_overlap is not None
                else len(set(served_ids) & set(shadow_ids))
            )

        if isinstance(rank_delta, dict):
            for value in rank_delta.values():
                try:
                    position_deltas.append(abs(float(value)))
                except (TypeError, ValueError):
                    continue
        elif isinstance(rank_delta, list):
            for item in rank_delta:
                value = item.get("delta") if isinstance(item, dict) else item
                try:
                    position_deltas.append(abs(float(value)))
                except (TypeError, ValueError):
                    continue

        queue_wait = float(queue_wait_ms or 0)
        latency = float(latency_ms or 0)
        queue_waits.append(queue_wait)
        # “完成耗时”从提交到结束，包含排队和实际计算时间。
        durations.append(queue_wait + latency)

    return ShadowMetrics(
        available=True,
        missing_sources=list(SHADOW_MISSING_SOURCES),
        requests=len(rows),
        top_n_overlap_rate=(
            _ratio(overlap_count, overlap_denominator)
            if overlap_denominator else None
        ),
        average_position_delta=(
            round(sum(position_deltas) / len(position_deltas), 6)
            if position_deltas else None
        ),
        timeout_count=timeout_count,
        local_capacity_skip_count=local_capacity_skip_count,
        global_capacity_skip_count=global_capacity_skip_count,
        persistence_drop_count=None,
        queue_wait_p95_ms=percentile(queue_waits, 0.95),
        duration_p95_ms=percentile(durations, 0.95),
    )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def collect_metrics(
    db: Session,
    *,
    direction: str | None = None,
    days: int = 7,
    now: datetime | None = None,
) -> RecommendationMetricsResponse:
    """§11.9 的全量推荐指标。返回值只含聚合量，不含任何 viewer 标识。"""
    start, end = resolve_window(days, now)
    target_counts = _impression_counts_by_target(db, direction, start, end)
    exposure = _exposure_section(db, direction, start, end, target_counts)
    return RecommendationMetricsResponse(
        window=MetricsWindow(
            days=int(days),
            start_utc=start.isoformat() + "Z",
            end_utc=end.isoformat() + "Z",
            direction=direction,
            business_timezone=str(business_timezone()),
        ),
        requests=_request_section(db, direction, start, end),
        attempts=_attempt_section(db, direction, start, end),
        exposure=exposure,
        recall_pool=_recall_pool_section(db, direction, start, end, target_counts),
        clicks=_click_section(db, direction, start, end, exposure.impressions),
        delivery=_delivery_section(db, direction, start, end, end),
        llm=_llm_section(db, direction, start, end),
        shadow=_shadow_section(db, direction, start, end),
    )


def get_exposure_daily(
    db: Session,
    *,
    start: date,
    end: date,
    target_type: str | None = None,
) -> ExposureDailyResponse:
    """自然日曝光报表（真源 ``recommendation_exposure_daily``，§11.9）。

    ``stat_date`` 是 ``exposed_at`` 转 Asia/Shanghai 后的日期，由
    ``app.tasks.recommendation_exposure_reconcile`` 异步重算；这里只读不写。
    返回按 (日期, 目标类型) 汇总的结果，不下发单候选明细。
    """
    query = db.query(
        RecommendationExposureDaily.stat_date,
        RecommendationExposureDaily.target_type,
        func.count(RecommendationExposureDaily.target_id),
        func.coalesce(func.sum(RecommendationExposureDaily.impression_count), 0),
        func.coalesce(func.max(RecommendationExposureDaily.impression_count), 0),
    ).filter(
        RecommendationExposureDaily.stat_date >= start,
        RecommendationExposureDaily.stat_date <= end,
    )
    if target_type:
        query = query.filter(RecommendationExposureDaily.target_type == target_type)
    rows = query.group_by(
        RecommendationExposureDaily.stat_date,
        RecommendationExposureDaily.target_type,
    ).order_by(
        RecommendationExposureDaily.stat_date,
        RecommendationExposureDaily.target_type,
    ).all()
    points = [
        ExposureDailyPoint(
            stat_date=stat_date.isoformat() if hasattr(stat_date, "isoformat") else str(stat_date),
            target_type=str(row_target_type),
            candidates=int(candidates),
            impressions=int(impressions),
            max_candidate_impressions=int(max_impressions),
        )
        for stat_date, row_target_type, candidates, impressions, max_impressions in rows
    ]
    return ExposureDailyResponse(
        start=start.isoformat(),
        end=end.isoformat(),
        target_type=target_type,
        business_timezone=str(business_timezone()),
        points=points,
    )
