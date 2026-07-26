"""Exposure facts and rolling opportunity helpers."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Iterable

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.time_utils import ensure_utc, exposure_window_start, to_naive_utc, utc_now
from app.models import RecommendationDelivery, RecommendationImpression

# §9.6 的 delivery 状态枚举里没有 ``redacted``，它是写入侧正在移除的历史状态。
# 读取侧在迁移期继续接受它，否则历史行的曝光派生会永久停摆。
DERIVABLE_DELIVERY_STATUSES = ("sent", "redacted")

# §9.6 的 impression_state 枚举是 pending/processing/completed/retry。
# ``deriving`` 是 ``processing`` 的历史写法，claim 仍然识别它以便回收迁移前写下的
# 行；新代码一律只写 ``processing``。
IMPRESSION_IN_FLIGHT_STATES = ("processing", "deriving")
IMPRESSION_CLAIMABLE_STATES = ("pending", "retry", *IMPRESSION_IN_FLIGHT_STATES)

MAX_IMPRESSION_BACKOFF_SECONDS = 300
# 用户级重复控制只看冷却窗内“已 sent 但未派生完”的投递，正常情况下最多几条；
# 上限只是防御异常积压时把整个搜索拖慢。
PENDING_CONTEXT_SCAN_LIMIT = 200


def exposure_opportunities(counts: dict[str, int], candidate_ids: Iterable[str]) -> dict[str, float]:
    # 候选 id 去重后再取 n：重复 id 会让 (n - 1) 分母偏大，分位数整体被压低。
    ids: list[str] = []
    seen: set[str] = set()
    for item in candidate_ids:
        key = str(item)
        if key in seen:
            continue
        seen.add(key)
        ids.append(key)
    n = len(ids)
    if n <= 1:
        return {item: 0.5 for item in ids}
    values = {item: int(counts.get(item, 0) or 0) for item in ids}
    result: dict[str, float] = {}
    for item, count in values.items():
        lower = sum(value < count for value in values.values())
        equal = sum(value == count for value in values.values())
        percentile = (lower + 0.5 * (equal - 1)) / (n - 1)
        result[item] = 1 - percentile
    return result


def batch_candidate_exposures(
    db: Session,
    *,
    target_type: str,
    candidate_ids: Iterable[str | int],
    request_now_utc: datetime,
    window_hours: int = 168,
) -> dict[str, int]:
    """§6.6 的全局曝光机会分只读 impression 事实表，允许最多 2 秒派生延迟。"""
    ids = _int_ids(candidate_ids)
    if not ids:
        return {}
    now = ensure_utc(request_now_utc) or utc_now()
    start = exposure_window_start(now, hours=window_hours)
    rows = (
        db.query(
            RecommendationImpression.target_id,
            func.count(RecommendationImpression.id),
        )
        .filter(
            RecommendationImpression.target_type == target_type,
            RecommendationImpression.target_id.in_(ids),
            RecommendationImpression.exposed_at >= to_naive_utc(start),
            RecommendationImpression.exposed_at < to_naive_utc(now),
        )
        .group_by(RecommendationImpression.target_id)
        .all()
    )
    return {str(target_id): int(count) for target_id, count in rows}


def recent_user_exposures(
    db: Session,
    *,
    viewer_userid: str,
    target_type: str,
    candidate_ids: Iterable[str | int],
    request_now_utc: datetime,
    cooldown_hours: int,
) -> dict[str, datetime]:
    """§10.5：用户级重复控制不允许有派生延迟窗口。

    因此这里必须合并两个来源：已落 ``recommendation_impression`` 的事实，以及同
    viewer、已 sent 但 ``impression_state != completed`` 的 delivery context。按
    ``(delivery_id, target_type, target_id)`` 去重后取最近一次曝光时间，背靠背搜索
    的第二条请求才会把第一条刚发出去的推荐视为看过。
    """
    ids = _int_ids(candidate_ids)
    if not ids or cooldown_hours <= 0:
        return {}
    now = ensure_utc(request_now_utc) or utc_now()
    start = exposure_window_start(now, hours=cooldown_hours)
    window_start = to_naive_utc(start)
    window_end = to_naive_utc(now)
    id_set = set(ids)
    # (delivery_id, target_type, target_id) -> 曝光时刻
    exposures: dict[tuple[str, str, int], datetime] = {}

    impression_rows = (
        db.query(
            RecommendationImpression.delivery_id,
            RecommendationImpression.target_id,
            func.max(RecommendationImpression.exposed_at),
        )
        .filter(
            RecommendationImpression.viewer_userid == viewer_userid,
            RecommendationImpression.target_type == target_type,
            RecommendationImpression.target_id.in_(ids),
            RecommendationImpression.exposed_at >= window_start,
            RecommendationImpression.exposed_at < window_end,
        )
        .group_by(
            RecommendationImpression.delivery_id,
            RecommendationImpression.target_id,
        )
        .all()
    )
    for delivery_id, target_id, exposed_at in impression_rows:
        instant = ensure_utc(exposed_at)
        if instant is None:
            continue
        exposures[(str(delivery_id), target_type, int(target_id))] = instant

    pending_rows = (
        db.query(
            RecommendationDelivery.delivery_id,
            RecommendationDelivery.recommendation_context,
            RecommendationDelivery.sent_at,
        )
        .filter(
            RecommendationDelivery.userid == viewer_userid,
            RecommendationDelivery.status.in_(DERIVABLE_DELIVERY_STATUSES),
            RecommendationDelivery.impression_state != "completed",
            RecommendationDelivery.sent_at.isnot(None),
            RecommendationDelivery.sent_at >= window_start,
            RecommendationDelivery.sent_at < window_end,
        )
        .order_by(RecommendationDelivery.sent_at.desc())
        .limit(PENDING_CONTEXT_SCAN_LIMIT)
        .all()
    )
    for delivery_id, context, sent_at in pending_rows:
        instant = ensure_utc(sent_at)
        if instant is None:
            continue
        for item in _context_items(_context_dict(context)):
            key = _item_key(item)
            if key is None or key[0] != target_type or key[1] not in id_set:
                continue
            dedup_key = (str(delivery_id), key[0], key[1])
            if dedup_key in exposures:
                # 同一投递已经有 impression 事实，事实表时间优先。
                continue
            exposures[dedup_key] = instant

    result: dict[str, datetime] = {}
    for (_delivery_id, _target_type, target_id), instant in exposures.items():
        current = result.get(str(target_id))
        if current is None or instant > current:
            result[str(target_id)] = instant
    return result


def derive_impressions(db: Session, delivery: RecommendationDelivery, *, exposed_at: datetime | None = None) -> int:
    """Idempotently materialize all items from a sent delivery (§10.5)."""
    if delivery.status not in DERIVABLE_DELIVERY_STATUSES:
        return 0
    context = _context_dict(delivery.recommendation_context)
    items = _context_items(context)
    moment = ensure_utc(exposed_at) or _sent_instant(delivery) or utc_now()
    naive_moment = to_naive_utc(moment)
    inserted = 0
    existing_rows = db.query(
        RecommendationImpression.target_type,
        RecommendationImpression.target_id,
    ).filter(
        RecommendationImpression.delivery_id == delivery.delivery_id,
    ).all()
    existing_keys = {(row[0], int(row[1])) for row in existing_rows}
    expected_keys: set[tuple[str, int]] = set()
    for item in items:
        key = _item_key(item)
        if key is None:
            # 没有可用 target 的条目永远变不成 impression，算进预期数会让这条
            # delivery 永久停在 retry。
            continue
        if key in expected_keys:
            continue
        expected_keys.add(key)
        if key in existing_keys:
            continue
        target_type, target_id = key
        db.add(RecommendationImpression(
            delivery_id=delivery.delivery_id,
            request_id=delivery.request_id,
            snapshot_id=delivery.snapshot_id or "",
            viewer_userid=delivery.userid,
            direction=context.get("direction", ""),
            target_type=target_type,
            target_id=target_id,
            position=int(item.get("position", 0) or 0),
            strategy_version_id=context.get("strategy_version_id"),
            algorithm_version=context.get("algorithm_version", "legacy"),
            assignment=context.get("assignment", "legacy"),
            is_exploration=bool(item.get("is_exploration", False)),
            query_digest=context.get("query_digest", ""),
            score_detail=item.get("score_detail"),
            exposed_at=naive_moment,
        ))
        existing_keys.add(key)
        inserted += 1
    db.flush()
    expected = len(expected_keys)
    actual = db.query(RecommendationImpression.id).filter(
        RecommendationImpression.delivery_id == delivery.delivery_id,
    ).count()
    delivery.impression_expected_count = expected
    delivery.impression_actual_count = actual
    # §10.5：只有实际数量严格等于预期数才能写 completed；预期数为 0 直接完成。
    if expected == 0 or actual == expected:
        delivery.impression_state = "completed"
        delivery.impression_derived_at = naive_moment
        delivery.impression_last_error = None
    else:
        _schedule_impression_retry(
            delivery,
            error_text=f"impression count mismatch expected={expected} actual={actual}",
        )
    _release_impression_lease(delivery)
    return inserted


def claim_impression_deliveries(
    db: Session, *, limit: int = 50, lease_seconds: int = 30, owner: str = "impression-worker",
) -> list[str]:
    """§10.5：claim 一批待派生 delivery，立刻置为 processing 并占用派生租约。

    行锁只在 claim 事务内有效，commit 之后唯一的并发保护就是租约列，所以过滤条件
    必须同时排除“正在派生且租约未过期”的行。租约用 ``impression_lease_*``：发送侧
    的 ``lease_owner``/``lease_expires_at`` 属于 outbox dispatcher，派生任务既不读也
    不写，否则会把一条 ``sending`` 中的 delivery 让给第二个 dispatcher 重复发送。
    """
    now = utc_now()
    now_naive = to_naive_utc(now)
    lease_until = to_naive_utc(now + timedelta(seconds=lease_seconds))
    rows = db.query(RecommendationDelivery).filter(
        RecommendationDelivery.status.in_(DERIVABLE_DELIVERY_STATUSES),
        RecommendationDelivery.impression_state.in_(IMPRESSION_CLAIMABLE_STATES),
        RecommendationDelivery.impression_next_attempt_at <= now_naive,
        or_(
            RecommendationDelivery.impression_lease_expires_at.is_(None),
            RecommendationDelivery.impression_lease_expires_at <= now_naive,
        ),
    ).order_by(
        RecommendationDelivery.impression_next_attempt_at,
        RecommendationDelivery.delivery_id,
    ).with_for_update(skip_locked=True).limit(limit).all()
    claimed: list[str] = []
    for row in rows:
        if row.impression_state in IMPRESSION_IN_FLIGHT_STATES:
            # §10.5「processing lease 过期转 retry」：能被领到就说明上一个持有者的
            # 租约已经过期，那次尝试记为失败；这里直接接管，省掉一次
            # processing → retry → processing 的往返。
            row.impression_attempt_count = int(row.impression_attempt_count or 0) + 1
            row.impression_last_error = "impression lease expired; reclaimed"
        row.impression_state = "processing"
        row.impression_lease_owner = owner
        row.impression_lease_expires_at = lease_until
        claimed.append(row.delivery_id)
    return claimed


def mark_impression_retry(db: Session, delivery_id: str, error: Exception) -> None:
    delivery = db.get(RecommendationDelivery, delivery_id)
    if not delivery:
        return
    _schedule_impression_retry(delivery, error_text=f"{type(error).__name__}: {error}")
    _release_impression_lease(delivery)


def _schedule_impression_retry(delivery: RecommendationDelivery, *, error_text: str | None) -> None:
    """§10.5：写 retry、递增 attempt count 并设置退避时间。"""
    attempt = int(delivery.impression_attempt_count or 0) + 1
    delivery.impression_attempt_count = attempt
    delivery.impression_state = "retry"
    delivery.impression_next_attempt_at = to_naive_utc(
        utc_now() + timedelta(seconds=_impression_backoff_seconds(attempt)),
    )
    if error_text:
        delivery.impression_last_error = error_text[:500]


def _release_impression_lease(delivery: RecommendationDelivery) -> None:
    """只释放派生租约。

    发送租约 ``lease_owner``/``lease_expires_at`` 由 outbox dispatcher 独占（§9.6）：
    派生成功或失败都不得清空它，否则一条还在 ``sending`` 的 delivery 会被第二个
    dispatcher 重新 claim，造成企微重复发送。
    """
    delivery.impression_lease_owner = None
    delivery.impression_lease_expires_at = None


def _impression_backoff_seconds(attempt_count: int) -> int:
    return min(MAX_IMPRESSION_BACKOFF_SECONDS, 2 ** min(max(int(attempt_count or 0), 1), 8))


def _int_ids(candidate_ids: Iterable[str | int]) -> list[int]:
    ids: list[int] = []
    for item in candidate_ids:
        try:
            ids.append(int(item))
        except (TypeError, ValueError):
            continue
    return ids


def _context_dict(context: Any) -> dict:
    if isinstance(context, str):
        try:
            context = json.loads(context)
        except ValueError:
            return {}
    return context if isinstance(context, dict) else {}


def _context_items(context: dict) -> list[dict]:
    items = context.get("items")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _item_key(item: dict) -> tuple[str, int] | None:
    target_type = item.get("target_type")
    if not isinstance(target_type, str) or not target_type:
        return None
    try:
        return target_type, int(item["target_id"])
    except (KeyError, TypeError, ValueError):
        return None


def _sent_instant(delivery: RecommendationDelivery) -> datetime | None:
    """曝光时间以真正的发送时间为准，避免异步派生把时间点往后拖。"""
    value = delivery.sent_at
    return ensure_utc(value) if isinstance(value, datetime) else None
