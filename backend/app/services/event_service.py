"""事件回传 service（Phase 5 模块 J，v0.7 §9.9 点击归因）。

记录小程序点击等外部事件：

- 新版客户端带 `delivery_id`：服务端反查**持久投递和曝光事实**，反向校验
  userid/target_type/target_id 后才写归因字段（§9.9）。曝光事实是唯一真源，
  没有 impression 就不归因，避免 CTR 分子先于分母出现；
- 上下文不匹配：写 `attribution_status=rejected` 行 + 安全日志，并向调用方抛
  业务错误，绝不伪造归因（§9.9）；
- 幂等：归因点击用 `attribution_dedupe_key` 唯一键兜底；老客户端没有 delivery ID，
  沿用 `event.dedupe_window_seconds` 的 Redis 时间窗，记 `legacy_unattributed`，
  不进入策略 CTR；
- 时间口径：客户端 `timestamp` 一律转 UTC 后按 naive UTC 落库（§9.12）；
- 失败降级：event_log 写入失败时记 audit_log，不阻塞业务回包。
"""
from __future__ import annotations

import logging
import hashlib
from datetime import datetime, timezone
from typing import NamedTuple

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.core.redis_client import (
    EVENT_DEDUPE_TTL_DEFAULT,
    clear_event_idem,
    mark_event_idem,
)
from app.core.logging_setup import identifier_hash
from app.core.time_utils import to_naive_utc, utc_now
from app.models import (
    EventLog,
    RecommendationDelivery,
    RecommendationImpression,
    SystemConfig,
)
from app.services.admin_log_service import write_admin_log
from app.tasks.common import log_event

logger = logging.getLogger(__name__)

CLICK_EVENT_TYPE = "miniprogram_click"

# event_log.attribution_status 枚举（§9.9）
ATTRIBUTED = "attributed"
LEGACY_UNATTRIBUTED = "legacy_unattributed"
REJECTED = "rejected"


class ClickAttributionRejected(ValueError):
    """归因上下文校验失败。

    §9.9：上下文不匹配时返回业务错误并记录安全日志，不允许伪造归因。
    继承 `ValueError` 是为了兼容既有 API 层的 `except ValueError` 兜底。
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__("invalid recommendation click attribution")


class ClickResult(NamedTuple):
    """`record_click` 返回值。

    - `deduped`：True 表示命中幂等（Redis 时间窗或归因唯一键），未重复写库
    - `attribution_status`：本次点击最终落库的归因状态
    """

    deduped: bool
    attribution_status: str


def build_attribution_dedupe_key(
    event_type: str,
    delivery_id: str,
    target_type: str,
    target_id: int | str,
) -> str:
    """§9.9 数据库幂等合同。

        attribution_dedupe_key =
        SHA256(event_type + "|" + delivery_id + "|" + target_type + "|" + target_id)

    拼接顺序和分隔符是唯一键语义的一部分，改动会让历史行与新行不再互斥。
    """
    raw = "|".join([event_type, delivery_id, target_type, str(target_id)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _get_dedupe_ttl(db: Session) -> int:
    cfg = db.query(SystemConfig).filter(
        SystemConfig.config_key == "event.dedupe_window_seconds",
    ).first()
    if not cfg:
        return EVENT_DEDUPE_TTL_DEFAULT
    try:
        return int(cfg.config_value)
    except (TypeError, ValueError):
        return EVENT_DEDUPE_TTL_DEFAULT


def _click_occurred_at(timestamp: int | None) -> datetime:
    """客户端上报时间 → naive UTC（§9.12：点击 timestamp 转 UTC 后保存）。

    兼容客户端既可能发秒（UNIX 常规）也可能发毫秒（JS Date.now()）：
    大于 10^12 视为毫秒，除以 1000 规整为秒。epoch 本身就是 UTC 刻度，
    这里显式带上 tz 再转 naive，避免 `fromtimestamp()` 落成宿主机本地时间。
    """
    if timestamp:
        ts = int(timestamp)
        if ts > 10 ** 12:
            ts = ts // 1000
        try:
            moment = datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            moment = utc_now()
    else:
        moment = utc_now()
    return to_naive_utc(moment)  # type: ignore[return-value]


def _clear_idem(userid: str, target_type: str, target_id: int) -> None:
    """释放 Redis 幂等 key，允许下次同事件重试写库。"""
    try:
        clear_event_idem(userid, target_type, target_id)
    except Exception:
        logger.exception("event_service: clear_event_idem failed")


def _write_failure_audit(
    db: Session,
    *,
    userid: str,
    target_type: str,
    target_id: int,
    exc: Exception,
) -> None:
    """event_log 写库失败的兜底：写 audit_log，不阻塞响应。"""
    logger.exception(
        "event_service: event_log write failed user_hash=%s",
        identifier_hash(userid),
    )
    try:
        write_admin_log(
            db,
            target_type="user", target_id=userid,
            action="auto_reject", operator="system",
            before=None,
            after={"target_type": target_type, "target_id": target_id},
            reason=f"event_log write failed: {exc}",
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("event_service: fallback audit_log write failed")


def _attribution_reject_reason(
    *,
    delivery: RecommendationDelivery | None,
    impression: RecommendationImpression | None,
    userid: str,
    request_id: str | None,
    snapshot_id: str | None,
) -> str | None:
    """反向校验点击上下文；返回拒绝原因，None 表示校验通过。

    §9.9 只要求校验 userid/target_type/target_id（target 已经是 impression 的
    查询条件），客户端另外带上 request/snapshot 时一并核对，不一致同样视为伪造。
    """
    if delivery is None:
        return "delivery_not_found"
    if delivery.userid != userid:
        return "delivery_userid_mismatch"
    if request_id and request_id != delivery.request_id:
        return "request_id_mismatch"
    if snapshot_id and snapshot_id != (delivery.snapshot_id or ""):
        return "snapshot_id_mismatch"
    # 曝光事实表是唯一真源（§9.5）。没有 impression 说明这条投递还没真正发出去
    # 或还没派生，此时归因会让 CTR 分子先于分母出现——按 §9.9 拒绝，不允许用
    # “最近一次曝光”之类的时间窗补猜。
    if impression is None:
        return "impression_not_found"
    if impression.viewer_userid != userid:
        return "impression_viewer_mismatch"
    return None


def _attribution_already_recorded(db: Session, dedupe_key: str) -> bool:
    """归因幂等快路径查询（真正的互斥由 `attribution_dedupe_key` 唯一键保证）。"""
    return db.query(EventLog.id).filter(
        EventLog.attribution_dedupe_key == dedupe_key,
    ).first() is not None


def _persist_rejected_click(
    db: Session,
    *,
    userid: str,
    target_type: str,
    target_id: int,
    occurred_at: datetime,
    delivery_id: str,
    request_id: str | None,
    snapshot_id: str | None,
    position: int | None,
    client_event_id: str | None,
    reason: str,
) -> None:
    """记录安全日志并把 `rejected` 事实落库（§9.9）。

    刻意**不写** `attribution_dedupe_key`：该键是「这条投递的这个目标已归因」的
    唯一键，若被拒绝行占用，只要拿别人的 delivery_id 报一次假点击就能把真实用户
    的归因永久顶掉。拒绝行保留客户端**声称**的上下文，供事后追查。
    """
    log_event(
        "recommendation_click_attribution_rejected",
        severity="warning",
        reason=reason,
        user_hash=identifier_hash(userid),
        delivery_id=delivery_id,
        request_id=request_id,
        snapshot_id=snapshot_id,
        target_type=target_type,
        target_id=target_id,
        position=position,
    )
    try:
        db.add(EventLog(
            event_type=CLICK_EVENT_TYPE,
            userid=userid,
            target_type=target_type,
            target_id=target_id,
            occurred_at=occurred_at,
            delivery_id=delivery_id,
            request_id=request_id,
            snapshot_id=snapshot_id,
            position=position,
            attribution_status=REJECTED,
            client_event_id=client_event_id,
            attribution_dedupe_key=None,
            extra={"reject_reason": reason},
        ))
        db.commit()
    except IntegrityError:
        # (userid, event_type, client_event_id) 唯一键：同一次拒绝被重放，幂等即可
        db.rollback()
    except Exception:
        db.rollback()
        logger.exception(
            "event_service: rejected click persist failed user_hash=%s",
            identifier_hash(userid),
        )


def _record_attributed_click(
    db: Session,
    *,
    userid: str,
    target_type: str,
    target_id: int,
    occurred_at: datetime,
    delivery_id: str,
    request_id: str | None,
    snapshot_id: str | None,
    position: int | None,
    client_event_id: str | None,
) -> ClickResult:
    """新版客户端链路：反查投递 + 曝光事实后写归因点击。"""
    delivery = db.get(RecommendationDelivery, delivery_id)
    # (delivery_id, target_type, target_id) 上有唯一键，最多一行
    impression = db.query(RecommendationImpression).filter(
        RecommendationImpression.delivery_id == delivery_id,
        RecommendationImpression.target_type == target_type,
        RecommendationImpression.target_id == target_id,
    ).first()

    reason = _attribution_reject_reason(
        delivery=delivery,
        impression=impression,
        userid=userid,
        request_id=request_id,
        snapshot_id=snapshot_id,
    )
    if reason:
        _persist_rejected_click(
            db,
            userid=userid, target_type=target_type, target_id=target_id,
            occurred_at=occurred_at, delivery_id=delivery_id,
            request_id=request_id, snapshot_id=snapshot_id, position=position,
            client_event_id=client_event_id, reason=reason,
        )
        raise ClickAttributionRejected(reason)

    # 走到这里 delivery/impression 必然存在：`_attribution_reject_reason` 已经兜住
    dedupe_key = build_attribution_dedupe_key(
        CLICK_EVENT_TYPE, delivery_id, target_type, target_id,
    )
    # 快路径：命中直接幂等返回。这里先查后插仍有竞态，唯一键是最终兜底。
    if _attribution_already_recorded(db, dedupe_key):
        return ClickResult(deduped=True, attribution_status=ATTRIBUTED)

    # 归因字段一律取曝光事实，不取客户端上报值，也不取 delivery 的 JSON context
    try:
        db.add(EventLog(
            event_type=CLICK_EVENT_TYPE,
            userid=userid,
            target_type=target_type,
            target_id=target_id,
            occurred_at=occurred_at,
            delivery_id=delivery_id,
            request_id=impression.request_id,
            snapshot_id=impression.snapshot_id or None,
            position=int(impression.position),
            attribution_status=ATTRIBUTED,
            attributed_strategy_version_id=impression.strategy_version_id,
            attributed_algorithm_version=impression.algorithm_version,
            attributed_is_exploration=bool(impression.is_exploration),
            client_event_id=client_event_id,
            attribution_dedupe_key=dedupe_key,
        ))
        db.commit()
    except IntegrityError:
        # 并发下同一归因/同一 client_event_id 重复插入：幂等返回已有事件
        db.rollback()
        return ClickResult(deduped=True, attribution_status=ATTRIBUTED)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        _write_failure_audit(
            db, userid=userid, target_type=target_type, target_id=target_id, exc=exc,
        )
    return ClickResult(deduped=False, attribution_status=ATTRIBUTED)


def _record_legacy_click(
    db: Session,
    *,
    userid: str,
    target_type: str,
    target_id: int,
    occurred_at: datetime,
    request_id: str | None,
    snapshot_id: str | None,
    position: int | None,
    client_event_id: str | None,
) -> ClickResult:
    """老客户端链路：没有 delivery ID，点击仍保存但不进入策略 CTR（§9.9）。"""
    ttl = _get_dedupe_ttl(db)
    try:
        first = mark_event_idem(userid, target_type, target_id, ttl=ttl)
    except Exception:
        logger.exception("event_service: redis mark_event_idem failed (fallback to DB write)")
        first = True  # fail-open，允许写库

    if not first:
        return ClickResult(deduped=True, attribution_status=LEGACY_UNATTRIBUTED)

    try:
        db.add(EventLog(
            event_type=CLICK_EVENT_TYPE,
            userid=userid,
            target_type=target_type,
            target_id=target_id,
            occurred_at=occurred_at,
            request_id=request_id,
            snapshot_id=snapshot_id,
            position=position,
            attribution_status=LEGACY_UNATTRIBUTED,
            client_event_id=client_event_id,
            attribution_dedupe_key=None,
        ))
        db.commit()
    except IntegrityError:
        # (userid, event_type, client_event_id) 唯一键：重放，幂等返回
        db.rollback()
        return ClickResult(deduped=True, attribution_status=LEGACY_UNATTRIBUTED)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        _clear_idem(userid, target_type, target_id)
        _write_failure_audit(
            db, userid=userid, target_type=target_type, target_id=target_id, exc=exc,
        )
    return ClickResult(deduped=False, attribution_status=LEGACY_UNATTRIBUTED)


def record_click(
    db: Session,
    userid: str,
    target_type: str,
    target_id: int,
    timestamp: int | None = None,
    delivery_id: str | None = None,
    request_id: str | None = None,
    snapshot_id: str | None = None,
    position: int | None = None,
    client_event_id: str | None = None,
) -> ClickResult:
    """记录一次小程序点击事件。

    带 `delivery_id` 走 §9.9 归因链路，上下文校验失败抛
    `ClickAttributionRejected`；不带则按老客户端记 `legacy_unattributed`。
    """
    occurred_at = _click_occurred_at(timestamp)

    if delivery_id:
        return _record_attributed_click(
            db,
            userid=userid, target_type=target_type, target_id=target_id,
            occurred_at=occurred_at, delivery_id=delivery_id,
            request_id=request_id, snapshot_id=snapshot_id, position=position,
            client_event_id=client_event_id,
        )

    return _record_legacy_click(
        db,
        userid=userid, target_type=target_type, target_id=target_id,
        occurred_at=occurred_at,
        request_id=request_id, snapshot_id=snapshot_id, position=position,
        client_event_id=client_event_id,
    )
