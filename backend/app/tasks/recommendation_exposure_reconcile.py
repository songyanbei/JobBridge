"""``recommendation_exposure_daily`` 日聚合 / 重算任务（§9.8、§11.8）。

契约（§9.8 行 2037-2038）：

* 事实表 ``recommendation_impression`` 是唯一真源，聚合表可以随时重建；
* 聚合**只由异步任务**计算，不与事实写入放在同一事务，也不在发送链路同步
  upsert——因此本模块自己开 ``SessionLocal``，不接受外部事务里的 session；
* ``stat_date`` 按 ``exposed_at`` 转 Asia/Shanghai 后取日期（§9.12 行 2175），
  时区换算一律走 ``app.core.time_utils``，本模块不自己拼时区。

重算语义：``reconcile_day()`` 是**全量重算**而不是增量累加。一天的聚合被整体
重新计算后写回，同一 ``stat_date`` 下本轮没再出现的候选行会被删除（明细 TTL
清理或用户删除后，聚合不能留下孤儿行）。因此重复执行同一天必然收敛到同一结果，
可以安全地反复调用。

分批与事务：按 ``(target_type, target_id)`` keyset 翻页读事实表，每批
``BATCH_SIZE`` 行独立 commit，避免长事务把 impression 表压住。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.core.time_utils import business_date, business_day_bounds, to_naive_utc, utc_now
from app.db import SessionLocal
from app.models import RecommendationExposureDaily, RecommendationImpression
from app.tasks.common import log_event, task_lock

#: 单批聚合行数。一批 = 一次读 + 一次 upsert + 一次 commit。
BATCH_SIZE = 1000

#: 默认回补天数：今天 + 昨天。跨日任务在北京时间 00:0x 触发时，前一天的 sent
#: delivery 仍可能在派生队列里，所以昨天必须再算一遍。
DEFAULT_LOOKBACK_DAYS = 2

#: 单次 run() 允许重算的最大天数，防止手工传参把整表刷一遍。
MAX_LOOKBACK_DAYS = 90


def _upsert_batch(
    db: Session,
    day: date,
    rows: list[tuple[str | None, str, int, int]],
    updated_at: datetime,
) -> int:
    """按 scope-aware 聚合键 UPSERT 一批聚合行。

    ``updated_at`` 由调用方统一传入本轮的时间戳，``_purge_stale_rows()`` 靠它区分
    "本轮写过"和"上一轮遗留"，所以这里不能让 DB 的 ``onupdate`` 自己填。
    """
    if not rows:
        return 0
    payload = [
        {
            "stat_date": day,
            "demo_id": demo_id,
            "target_type": target_type,
            "target_id": target_id,
            # The aggregate table keeps legacy demo_id=NULL semantics while
            # using a non-null scope key for cross-dialect conflict handling.
            "scope_key": demo_id or "",
            "impression_count": count,
            "updated_at": updated_at,
        }
        for demo_id, target_type, target_id, count in rows
    ]
    dialect = db.get_bind().dialect.name
    table = RecommendationExposureDaily.__table__
    if dialect == "mysql":
        from sqlalchemy.dialects.mysql import insert as mysql_insert

        statement = mysql_insert(table).values(payload)
        statement = statement.on_duplicate_key_update(
            demo_id=statement.inserted.demo_id,
            impression_count=statement.inserted.impression_count,
            updated_at=statement.inserted.updated_at,
        )
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        statement = sqlite_insert(table).values(payload)
        statement = statement.on_conflict_do_update(
            index_elements=["stat_date", "target_type", "target_id", "scope_key"],
            set_={
                "demo_id": statement.excluded.demo_id,
                "impression_count": statement.excluded.impression_count,
                "updated_at": statement.excluded.updated_at,
            },
        )
    else:  # pragma: no cover - 仅在意外方言上兜底
        raise RuntimeError(f"unsupported dialect for exposure daily upsert: {dialect}")
    db.execute(statement)
    db.commit()
    return len(payload)


def _purge_stale_rows(db: Session, day: date, updated_at: datetime) -> int:
    """删除本轮没有写到的旧聚合行。

    明细被 TTL 清理或用户删除后，对应候选当天的曝光数应该归零而不是停在历史值；
    直接删行比写 0 更干净（聚合表只描述"有过曝光的候选"）。
    """
    total = 0
    while True:
        stale = db.query(
            RecommendationExposureDaily.demo_id,
            RecommendationExposureDaily.target_type,
            RecommendationExposureDaily.target_id,
        ).filter(
            RecommendationExposureDaily.stat_date == day,
            RecommendationExposureDaily.updated_at < updated_at,
        ).limit(BATCH_SIZE).all()
        if not stale:
            break
        deleted = db.query(RecommendationExposureDaily).filter(
            RecommendationExposureDaily.stat_date == day,
            or_(*(
                and_(
                    RecommendationExposureDaily.demo_id.is_(None)
                    if demo_id is None
                    else RecommendationExposureDaily.demo_id == str(demo_id),
                    RecommendationExposureDaily.target_type == str(target_type),
                    RecommendationExposureDaily.target_id == int(target_id),
                )
                for demo_id, target_type, target_id in stale
            )),
        ).delete(synchronize_session=False)
        db.commit()
        total += int(deleted or 0)
        # 防御：DELETE 一行没删掉说明选行条件与删除条件不一致，继续循环只会死转。
        if not deleted or len(stale) < BATCH_SIZE:
            break
    return total


def reconcile_day(db: Session, day: date, *, now: datetime | None = None) -> dict[str, Any]:
    """全量重算某个业务日（Asia/Shanghai 自然日）的曝光聚合。

    Args:
        db: 本任务独占的 session；函数内部会多次 commit，不要传业务事务里的 session。
        day: 业务日（``stat_date``），不是 UTC 日。
        now: 本轮时间戳，仅用于测试注入。

    Returns:
        ``{"stat_date", "rows", "impressions", "purged", "batches"}``。
    """
    start_utc, end_utc = business_day_bounds(day)
    start = to_naive_utc(start_utc)
    end = to_naive_utc(end_utc)
    stamp = to_naive_utc(now) or to_naive_utc(utc_now())

    rows_written = 0
    impressions = 0
    batches = 0
    cursor: tuple[str | None, str, int] | None = None
    while True:
        query = db.query(
            RecommendationImpression.demo_id,
            RecommendationImpression.target_type,
            RecommendationImpression.target_id,
            func.count(RecommendationImpression.id),
        ).filter(
            RecommendationImpression.exposed_at >= start,
            RecommendationImpression.exposed_at < end,
        )
        if cursor is not None:
            # keyset 翻页：OFFSET 会随天数线性劣化；NULL demo_id（legacy）
            # 按 ASC 排在显式 demo scope 前，三元组保持稳定有序。
            demo_id, target_type, target_id = cursor
            if demo_id is None:
                query = query.filter(or_(
                    RecommendationImpression.demo_id.isnot(None),
                    and_(
                        RecommendationImpression.demo_id.is_(None),
                        RecommendationImpression.target_type > target_type,
                    ),
                    and_(
                        RecommendationImpression.demo_id.is_(None),
                        RecommendationImpression.target_type == target_type,
                        RecommendationImpression.target_id > target_id,
                    ),
                ))
            else:
                query = query.filter(or_(
                    RecommendationImpression.demo_id > demo_id,
                    and_(
                        RecommendationImpression.demo_id == demo_id,
                        RecommendationImpression.target_type > target_type,
                    ),
                    and_(
                        RecommendationImpression.demo_id == demo_id,
                        RecommendationImpression.target_type == target_type,
                        RecommendationImpression.target_id > target_id,
                    ),
                ))
        batch = query.group_by(
            RecommendationImpression.demo_id,
            RecommendationImpression.target_type,
            RecommendationImpression.target_id,
        ).order_by(
            RecommendationImpression.demo_id,
            RecommendationImpression.target_type,
            RecommendationImpression.target_id,
        ).limit(BATCH_SIZE).all()
        if not batch:
            break
        normalized = [
            (str(demo_id) if demo_id is not None else None,
             str(target_type), int(target_id), int(count))
            for demo_id, target_type, target_id, count in batch
        ]
        rows_written += _upsert_batch(db, day, normalized, stamp)
        impressions += sum(item[3] for item in normalized)
        batches += 1
        cursor = normalized[-1][:3]
        if len(batch) < BATCH_SIZE:
            break

    purged = _purge_stale_rows(db, day, stamp)
    return {
        "stat_date": day.isoformat(),
        "rows": rows_written,
        "impressions": impressions,
        "purged": purged,
        "batches": batches,
    }


def reconcile_days(db: Session, days: list[date], *, now: datetime | None = None) -> list[dict[str, Any]]:
    """按天顺序重算多个业务日；单天失败不影响其它天。"""
    results: list[dict[str, Any]] = []
    for day in days:
        try:
            results.append(reconcile_day(db, day, now=now))
        except Exception:
            logger.exception(f"recommendation_exposure_reconcile: day failed stat_date={day.isoformat()}")
            db.rollback()
            results.append({"stat_date": day.isoformat(), "error": True})
    return results


def recent_business_days(lookback_days: int, *, now: datetime | None = None) -> list[date]:
    """最近 ``lookback_days`` 个业务日，按时间升序（含今天）。"""
    span = max(1, min(int(lookback_days), MAX_LOOKBACK_DAYS))
    today = business_date(now)
    return [today - timedelta(days=offset) for offset in range(span - 1, -1, -1)]


def run(lookback_days: int = DEFAULT_LOOKBACK_DAYS, *, now: datetime | None = None) -> None:
    """调度入口：重算最近 ``lookback_days`` 个业务日。

    默认重算今天 + 昨天：曝光派生本身是异步的，昨天的 sent delivery 可能在跨日
    之后才落 impression，只算"昨天一次"会永久少数。
    """
    with task_lock("recommendation_exposure_reconcile", ttl=1800) as acquired:
        if not acquired:
            logger.info("recommendation_exposure_reconcile: skipped, another instance holds the lock")
            return
        days = recent_business_days(lookback_days, now=now)
        with SessionLocal() as db:
            results = reconcile_days(db, days, now=now)
        log_event(
            "recommendation_exposure_reconcile_summary",
            days=len(days),
            rows=sum(int(item.get("rows") or 0) for item in results),
            impressions=sum(int(item.get("impressions") or 0) for item in results),
            purged=sum(int(item.get("purged") or 0) for item in results),
            failed=sum(1 for item in results if item.get("error")),
        )


def rebuild_range(start: date, end: date, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """手工重建区间（含首尾）。运维命令用，不走调度锁以外的额外门禁。

    聚合表可以随时重建（§9.8），所以这里不做"是否已存在"判断，直接按天重算。
    """
    if end < start:
        raise ValueError("end must not be earlier than start")
    span = (end - start).days + 1
    if span > MAX_LOOKBACK_DAYS:
        raise ValueError(f"range too wide: {span} days > {MAX_LOOKBACK_DAYS}")
    days = [start + timedelta(days=offset) for offset in range(span)]
    with SessionLocal() as db:
        return reconcile_days(db, days, now=now)
