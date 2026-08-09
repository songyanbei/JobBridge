"""推荐域「删除我的信息」闭环（方案 §9.11.1 / §10.1.1 / §14.12）。

这里是推荐数据删除的**唯一**入口。方案明确要求专门的
``delete_recommendation_user_data()`` 服务并按 §9.11 的外键顺序删除，不能只依赖
数据库级联（外键是 ``RESTRICT``，顺序错了直接被挡）。

两个阶段：

* 执行 ``/删除我的信息`` 命令的当下（§9.11.1 行 2147）：
  :func:`redact_user_recommendation_content` 立即清空该 userid 名下所有 delivery
  的正文密文与 prepared session patch，并把 TTL 缩到当前时刻。不等延迟硬删。
* 延迟硬删到期后：:func:`delete_recommendation_user_data` 执行方案列出的七步，
  既清 viewer 侧事实，也清「别人看到过我」的候选侧事实。

约束（§9.11.1 行 2165、§14.12 行 3400）：

* 全流程幂等，重复执行结果不变；
* 分批删除，避免长事务；
* 日志只写随机批次 ID、表名和行数，**不写 userid、target ID、电话、正文或完整
  JSON**；
* 清理失败进入可观测重试队列（§10.1.1 行 2240），且失败也不得留下可解密正文；
* 时间一律走 :mod:`app.core.time_utils`。
"""
from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

from sqlalchemy.orm import Session

from app.core.redis_client import (
    RECOMMENDATION_SESSION_DELIVERY_INDEX_PREFIX as SESSION_DELIVERY_INDEX_PREFIX,
    RECOMMENDATION_SESSION_TARGET_INDEX_PREFIX as SESSION_TARGET_INDEX_PREFIX,
)
from app.core.time_utils import to_naive_utc, utc_now
from app.models import (
    ConversationLog,
    EventLog,
    Job,
    RecommendationDelivery,
    RecommendationExposureDaily,
    RecommendationImpression,
    RecommendationRequest,
    RecommendationSearchAttempt,
    Resume,
    WecomOutboundOutbox,
)

logger = logging.getLogger(__name__)

# 推荐正文被清理后 conversation_log 的固定占位符（§10.1.1 表格 / §14.12）。
REDACTED_PLACEHOLDER = "[推荐内容已清理]"

BATCH_SIZE = 500

# Redis session 反查索引（§10.1.1 行 2228-2229）。这两个 key 由 session mutation
# 的写入侧维护；本模块只读取并在清理后删除索引本身。方案明令**禁止**用全库
# ``KEYS`` 扫描兜底，所以索引缺失时就只清理能定位到的会话。
# 可观测重试队列（§10.1.1 行 2240）。
PRIVACY_RETRY_QUEUE = "queue:recommendation_privacy_retry"
PRIVACY_RETRY_DEAD_QUEUE = "queue:recommendation_privacy_retry:dead"
PRIVACY_RETRY_MAX_ATTEMPTS = 5

# 推荐方向与候选类型的映射。展平的 ID 列表（served_top_ids/candidate_ids/...）里
# 只有裸 ID，没有类型；靠 request.direction 判断这批 ID 到底是岗位还是简历，
# 避免「岗位 7」和「简历 7」互相误删。
_TARGET_TYPE_BY_DIRECTION = {
    "search_job": "job",
    "search_worker": "resume",
}

# 正文被清空后 delivery 不能再停在「还能重试」的状态，否则会变成正文已消失却仍被
# dispatcher 反复 claim 的僵尸行。映射只使用 §9.6 行 1921 的合法枚举
# （prepared/pending/sending/retry_wait/sent/permanent_failed/unknown）：
#   - 尚未确认发出的一律 permanent_failed（没有正文就永远发不出去了）；
#   - sending 的结果不可知，只能 unknown；
#   - sent 是终态，清正文不改状态 —— 投递状态和正文是否还在是两件事。
_STATUS_AFTER_REDACTION = {
    "prepared": "permanent_failed",
    "pending": "permanent_failed",
    "retry_wait": "permanent_failed",
    "sending": "unknown",
}

_REDACTION_REASON = "content redacted by privacy deletion"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TargetRef:
    """一个候选目标（该用户拥有的岗位或简历）。"""

    target_type: str
    target_id: int


@dataclass
class PrivacyReport:
    """一次删除闭环的可观测结果。

    ``batch_id`` 是随机值，是允许写进日志的唯一标识（§14.12 行 3400）。
    """

    batch_id: str
    rows: dict[str, int] = field(default_factory=dict)
    failed_steps: list[str] = field(default_factory=list)
    # 扫描口径，不是删除行数，所以不进 ``rows``/``total_rows``。
    targets: int = 0
    redacted_deliveries: int = 0

    @property
    def ok(self) -> bool:
        return not self.failed_steps

    @property
    def total_rows(self) -> int:
        return sum(self.rows.values())

    def add(self, step: str, count: int) -> None:
        self.rows[step] = self.rows.get(step, 0) + int(count or 0)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _new_batch_id() -> str:
    return secrets.token_hex(8)


def _chunks(values: Sequence[Any], size: int = BATCH_SIZE) -> Iterable[list]:
    for start in range(0, len(values), size):
        yield list(values[start:start + size])


def _settle(db: Session, commit: bool) -> None:
    """按调用方的事务边界收口一个批次。

    延迟硬删任务自己持有 session，按批 commit 避免长事务；命令路径（``/删除我的
    信息``）跑在调用方事务里，只 flush。
    """
    if commit:
        db.commit()
    else:
        db.flush()


def _as_dict(value: Any) -> dict:
    """JSON 列可能是 dict，也可能是驱动返回的字符串。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _as_list(value: Any) -> list | None:
    if isinstance(value, list):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return None
        return decoded if isinstance(decoded, list) else None
    return None


def _target_map(targets: Iterable[TargetRef]) -> dict[str, set[str]]:
    grouped: dict[str, set[str]] = {}
    for target in targets:
        grouped.setdefault(target.target_type, set()).add(str(target.target_id))
    return grouped


def _removable_ids(grouped: dict[str, set[str]], target_type: str | None) -> set[str]:
    """某个方向下应当从裸 ID 列表中剔除的字符串集合。

    方向未知时退化为「所有类型的 ID 都剔除」：宁可多删一个候选 ID，也不能把被删
    用户的候选留在别人的快照里。
    """
    removable: set[str] = set()
    for kind, ids in grouped.items():
        if target_type is None or kind == target_type:
            removable |= ids
        # 带类型前缀的写法（如 "job:12"）无歧义，任何方向都能安全剔除。
        removable |= {f"{kind}:{value}" for value in ids}
    return removable


def _strip_ids(value: Any, removable: set[str]) -> tuple[Any, bool]:
    """从裸 ID 列表里剔除被删候选，返回 ``(新值, 是否变化)``。"""
    items = _as_list(value)
    if items is None:
        return value, False
    kept = [item for item in items if str(item) not in removable]
    if len(kept) == len(items):
        return items, False
    return kept, True


def _strip_rank_delta(value: Any, removable: set[str]) -> tuple[Any, bool]:
    """``shadow_rank_delta`` 既可能是 {target_id: delta} 也可能是对象数组。"""
    if isinstance(value, dict):
        kept = {k: v for k, v in value.items() if str(k) not in removable}
        return kept, len(kept) != len(value)
    items = _as_list(value)
    if items is None:
        return value, False
    kept = []
    for item in items:
        if isinstance(item, dict):
            marker = item.get("target_id", item.get("id"))
            if marker is not None and str(marker) in removable:
                continue
        elif str(item) in removable:
            continue
        kept.append(item)
    return kept, len(kept) != len(items)


def _item_key(item: Any) -> tuple[str, int] | None:
    if not isinstance(item, dict):
        return None
    target_type = item.get("target_type")
    if not isinstance(target_type, str) or not target_type:
        return None
    try:
        return target_type, int(item["target_id"])
    except (KeyError, TypeError, ValueError):
        return None


def _log_batch(batch_id: str, step: str, table: str, rows: int) -> None:
    """删除批次日志：只有随机批次 ID、步骤、表名和行数（§14.12 行 3400）。"""
    if not rows:
        return
    logger.info(
        "recommendation_privacy: batch=%s step=%s table=%s rows=%d",
        batch_id, step, table, rows,
    )


# ---------------------------------------------------------------------------
# 可观测重试队列（§10.1.1 行 2240）
# ---------------------------------------------------------------------------

def enqueue_privacy_retry(
    external_userid: str,
    *,
    batch_id: str,
    failed_steps: Sequence[str],
    attempt: int = 0,
) -> bool:
    """把失败的清理压回重试队列；队列本身是运维可见的深度指标。"""
    payload = {
        "userid": external_userid,
        "batch_id": batch_id,
        "failed_steps": list(failed_steps),
        "attempt": int(attempt) + 1,
        "enqueued_at": utc_now().isoformat(),
    }
    queue = (
        PRIVACY_RETRY_QUEUE
        if payload["attempt"] < PRIVACY_RETRY_MAX_ATTEMPTS
        else PRIVACY_RETRY_DEAD_QUEUE
    )
    try:
        from app.core.redis_client import get_redis

        get_redis().rpush(queue, json.dumps(payload, ensure_ascii=False))
    except Exception as exc:
        # 队列不可用时不能吞掉信号，但日志里依旧不能出现 userid。
        logger.error(
            "recommendation_privacy: retry enqueue failed batch=%s queue=%s error=%s",
            batch_id, queue, type(exc).__name__,
        )
        return False
    logger.warning(
        "recommendation_privacy: cleanup requeued batch=%s queue=%s steps=%s attempt=%d",
        batch_id, queue, ",".join(failed_steps), payload["attempt"],
    )
    return True


def privacy_retry_depth() -> dict[str, int]:
    """重试队列深度，供 worker_monitor / 报表打点。"""
    try:
        from app.core.redis_client import get_redis

        r = get_redis()
        return {
            "pending": int(r.llen(PRIVACY_RETRY_QUEUE) or 0),
            "dead_letter": int(r.llen(PRIVACY_RETRY_DEAD_QUEUE) or 0),
        }
    except Exception:
        logger.exception("recommendation_privacy: retry depth probe failed")
        return {"pending": -1, "dead_letter": -1}


def pop_privacy_retry() -> dict | None:
    """取一条待重试任务；队列空或 Redis 不可用时返回 ``None``。"""
    try:
        from app.core.redis_client import get_redis

        raw = get_redis().lpop(PRIVACY_RETRY_QUEUE)
    except Exception:
        logger.exception("recommendation_privacy: retry pop failed")
        return None
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        payload = json.loads(raw)
    except ValueError:
        logger.warning("recommendation_privacy: dropped malformed retry payload")
        return None
    return payload if isinstance(payload, dict) else None


# ---------------------------------------------------------------------------
# 步骤 1：候选 target 清单
# ---------------------------------------------------------------------------

def owned_target_refs(db: Session, external_userid: str) -> list[TargetRef]:
    """该用户拥有的 resume / job target ID（§9.11.1 步骤 1）。

    含软删行：软删只是把 ``deleted_at`` 写上，行还在，仍然可能被别人的 delivery
    引用；必须一起纳入清理范围。
    """
    targets: list[TargetRef] = []
    for model, target_type in ((Resume, "resume"), (Job, "job")):
        rows = db.query(model.id).filter(model.owner_userid == external_userid).all()
        targets.extend(TargetRef(target_type, int(row[0])) for row in rows)
    return targets


def merge_target_refs(*groups: Iterable[TargetRef]) -> list[TargetRef]:
    merged: dict[tuple[str, int], TargetRef] = {}
    for group in groups:
        for target in group:
            merged[(target.target_type, target.target_id)] = target
    return list(merged.values())


# ---------------------------------------------------------------------------
# delivery 正文清理
# ---------------------------------------------------------------------------

def _redact_delivery_row(delivery: RecommendationDelivery, now: datetime) -> bool:
    """清空一行 delivery 的正文与 session patch，并落终态。返回是否有变化。"""
    naive_now = to_naive_utc(now)
    changed = False
    if delivery.content_ciphertext is not None:
        delivery.content_ciphertext = None
        changed = True
    if delivery.session_patch_ciphertext is not None:
        delivery.session_patch_ciphertext = None
        changed = True
    if delivery.content_expires_at is None or delivery.content_expires_at > naive_now:
        delivery.content_expires_at = naive_now
        changed = True
    new_status = _STATUS_AFTER_REDACTION.get(delivery.status)
    if new_status and new_status != delivery.status:
        delivery.status = new_status
        delivery.last_error = _REDACTION_REASON
        delivery.lease_owner = None
        delivery.lease_expires_at = None
        changed = True
    return changed


def _fail_pending_outbox(db: Session, delivery_ids: Sequence[str]) -> int:
    """正文没了就不可能再发出去，pending outbox 必须落 dead_letter。"""
    total = 0
    for chunk in _chunks(list(delivery_ids)):
        total += db.query(WecomOutboundOutbox).filter(
            WecomOutboundOutbox.recommendation_delivery_id.in_(chunk),
            WecomOutboundOutbox.status == "pending",
        ).update(
            {
                "status": "dead_letter",
                "locked_at": None,
                "next_attempt_at": None,
                "last_error": _REDACTION_REASON,
            },
            synchronize_session=False,
        )
    return int(total or 0)


def redact_user_recommendation_content(
    db: Session,
    external_userid: str,
    *,
    now: datetime | None = None,
    batch_id: str | None = None,
    commit: bool = False,
) -> int:
    """§9.11.1 行 2147：执行删除命令时**立即**清空该 userid 的正文与 session patch。

    只碰正文/密文/TTL/状态，不删任何事实行——事实行留给延迟硬删的
    :func:`delete_recommendation_user_data`，删除仍有 N 天的可撤回窗口。
    """
    moment = now or utc_now()
    trace = batch_id or _new_batch_id()
    redacted = 0
    touched: list[str] = []
    # 过滤条件只挑「本批一定会被改写」的行：还有密文、还有 session patch，或状态
    # 仍可迁移。这样每批必然缩小候选集合，分批扫描不会在幂等重入时空转。
    pending_filter = (
        RecommendationDelivery.content_ciphertext.isnot(None)
        | RecommendationDelivery.session_patch_ciphertext.isnot(None)
        | RecommendationDelivery.status.in_(tuple(_STATUS_AFTER_REDACTION))
    )
    while True:
        rows = db.query(RecommendationDelivery).filter(
            RecommendationDelivery.userid == external_userid,
            pending_filter,
        ).limit(BATCH_SIZE).all()
        if not rows:
            break
        for delivery in rows:
            if _redact_delivery_row(delivery, moment):
                redacted += 1
                touched.append(delivery.delivery_id)
        _settle(db, commit)
        if len(rows) < BATCH_SIZE:
            break
    if touched:
        _fail_pending_outbox(db, touched)
        _settle(db, commit)
    _log_batch(trace, "immediate_redaction", "recommendation_delivery", redacted)
    return redacted


# ---------------------------------------------------------------------------
# 步骤 4：按候选反查他人 delivery
# ---------------------------------------------------------------------------

def _delivery_ids_referencing_targets(
    db: Session,
    grouped: dict[str, set[str]],
) -> set[str]:
    """找出 context 引用了这些 target 的 delivery。

    先走两条索引（impression 的 ``target_type/target_id``、event_log 的
    ``target_type/target_id``）拿到绝大多数 delivery；已派生的 delivery 一定有
    impression 行，所以只有「还没派生完」的那一小撮需要读 JSON context，
    用 ``impression_state != completed`` 把全表扫描收敛成一个有界集合。
    """
    found: set[str] = set()
    for target_type, ids in grouped.items():
        int_ids = [int(v) for v in ids]
        for chunk in _chunks(int_ids):
            found.update(
                str(row[0])
                for row in db.query(RecommendationImpression.delivery_id).filter(
                    RecommendationImpression.target_type == target_type,
                    RecommendationImpression.target_id.in_(chunk),
                ).all()
                if row[0]
            )
            found.update(
                str(row[0])
                for row in db.query(EventLog.delivery_id).filter(
                    EventLog.target_type == target_type,
                    EventLog.target_id.in_(chunk),
                    EventLog.delivery_id.isnot(None),
                ).all()
                if row[0]
            )
    pending = db.query(
        RecommendationDelivery.delivery_id,
        RecommendationDelivery.recommendation_context,
    ).filter(
        RecommendationDelivery.impression_state != "completed",
    ).all()
    for delivery_id, context in pending:
        for item in _as_dict(context).get("items") or []:
            key = _item_key(item)
            if key and str(key[1]) in grouped.get(key[0], set()):
                found.add(str(delivery_id))
                break
    return found


def _scrub_context(context: Any, grouped: dict[str, set[str]]) -> tuple[dict, bool]:
    """从 delivery context 中移除被删候选，返回 ``(新 context, 是否变化)``。"""
    data = dict(_as_dict(context))
    if not data:
        return data, False
    changed = False
    items = data.get("items")
    if isinstance(items, list):
        kept = []
        for item in items:
            key = _item_key(item)
            if key and str(key[1]) in grouped.get(key[0], set()):
                changed = True
                continue
            kept.append(item)
        if changed:
            data["items"] = kept
    removable = _removable_ids(
        grouped, _TARGET_TYPE_BY_DIRECTION.get(str(data.get("direction") or "")),
    )
    for key in (
        "candidate_ids", "precision_pool_ids", "served_top_ids",
        "shadow_top_ids", "shown_items",
    ):
        if key in data:
            new_value, hit = _strip_ids(data[key], removable)
            if hit:
                data[key] = new_value
                changed = True
    if "shadow_rank_delta" in data:
        new_value, hit = _strip_rank_delta(data["shadow_rank_delta"], removable)
        if hit:
            data["shadow_rank_delta"] = new_value
            changed = True
    if changed:
        data["position_count"] = len(data.get("items") or [])
    return data, changed


def _recount_impressions(db: Session, delivery: RecommendationDelivery) -> None:
    """按剩余 context 重算 expected/actual（§9.11.1 步骤 4）。

    与 ``recommendation_exposure_service.derive_impressions`` 用同一套口径：
    expected = context 中仍然可用的去重 target 数，actual = 实存 impression 行数。
    两者相等就落 completed，否则这条 delivery 会永远停在 retry。
    """
    context = _as_dict(delivery.recommendation_context)
    expected_keys = {
        key for key in (_item_key(item) for item in context.get("items") or [])
        if key is not None
    }
    actual = db.query(RecommendationImpression.id).filter(
        RecommendationImpression.delivery_id == delivery.delivery_id,
    ).count()
    delivery.impression_expected_count = len(expected_keys)
    delivery.impression_actual_count = int(actual)
    if int(actual) == len(expected_keys):
        delivery.impression_state = "completed"
        delivery.impression_last_error = None
        delivery.impression_lease_owner = None
        delivery.impression_lease_expires_at = None
        if delivery.impression_derived_at is None:
            delivery.impression_derived_at = to_naive_utc(utc_now())


def recount_delivery_impressions(
    db: Session,
    delivery_ids: Sequence[str],
    *,
    commit: bool = False,
) -> int:
    """删完 impression 事实后再对齐一次 expected/actual。

    §9.11.1 把「重算 count」写在步骤 4，但步骤 3 的 impression 删除发生在其后，
    所以真正的终值必须在删除结束后再算一遍，否则 delivery 会卡在 retry。
    """
    if not delivery_ids:
        return 0
    total = 0
    for chunk in _chunks(list(delivery_ids)):
        for delivery in db.query(RecommendationDelivery).filter(
            RecommendationDelivery.delivery_id.in_(chunk),
        ).all():
            _recount_impressions(db, delivery)
            total += 1
        _settle(db, commit)
    return total


def _scrub_request_facts(
    db: Session,
    request_ids: Sequence[str],
    grouped: dict[str, set[str]],
) -> int:
    """从 request/attempt 的 ID 列表里移除被删候选。

    ``served_top_ids`` 在 request 上，``candidate_ids``/``precision_pool_ids`` 在
    attempt 上；``shadow_top_ids``/``shadow_rank_delta`` 只在模型真的有该列时才处理，
    以便与仍在演进的 shadow 字段解耦。
    """
    changed = 0
    for chunk in _chunks(list(request_ids)):
        requests = db.query(RecommendationRequest).filter(
            RecommendationRequest.request_id.in_(chunk),
        ).all()
        for request in requests:
            removable = _removable_ids(
                grouped, _TARGET_TYPE_BY_DIRECTION.get(str(request.direction or "")),
            )
            hit = False
            for column in ("served_top_ids", "shadow_top_ids"):
                if not hasattr(request, column):
                    continue
                new_value, touched = _strip_ids(getattr(request, column), removable)
                if touched:
                    setattr(request, column, new_value)
                    hit = True
            if hasattr(request, "shadow_rank_delta"):
                new_value, touched = _strip_rank_delta(
                    request.shadow_rank_delta, removable,
                )
                if touched:
                    request.shadow_rank_delta = new_value
                    hit = True
            if hit:
                changed += 1
        attempts = db.query(RecommendationSearchAttempt).filter(
            RecommendationSearchAttempt.request_id.in_(chunk),
        ).all()
        directions = {r.request_id: r.direction for r in requests}
        for attempt in attempts:
            removable = _removable_ids(
                grouped,
                _TARGET_TYPE_BY_DIRECTION.get(
                    str(directions.get(attempt.request_id) or ""),
                ),
            )
            hit = False
            for column in ("candidate_ids", "precision_pool_ids", "shadow_top_ids"):
                if not hasattr(attempt, column):
                    continue
                new_value, touched = _strip_ids(getattr(attempt, column), removable)
                if touched:
                    setattr(attempt, column, new_value)
                    hit = True
            if hit:
                changed += 1
    return changed


def redact_deliveries_for_targets(
    db: Session,
    targets: Sequence[TargetRef],
    *,
    now: datetime | None = None,
    batch_id: str | None = None,
    commit: bool = False,
    exclude_userid: str | None = None,
) -> set[str]:
    """§9.11.1 步骤 4 / §10.1.1 行 2231-2234：清理引用被删候选的 delivery。

    返回被处理的 delivery ID 集合，供步骤 5 反查 conversation_log 与 Redis session。
    """
    grouped = _target_map(targets)
    if not grouped:
        return set()
    trace = batch_id or _new_batch_id()
    moment = now or utc_now()
    delivery_ids = _delivery_ids_referencing_targets(db, grouped)
    if not delivery_ids:
        return set()

    touched: set[str] = set()
    request_ids: set[str] = set()
    for chunk in _chunks(sorted(delivery_ids)):
        deliveries = db.query(RecommendationDelivery).filter(
            RecommendationDelivery.delivery_id.in_(chunk),
        ).all()
        for delivery in deliveries:
            if exclude_userid is not None and delivery.userid == exclude_userid:
                # 被删用户自己的 delivery 在步骤 2 整行删除，不用逐字段擦。
                continue
            context, context_changed = _scrub_context(
                delivery.recommendation_context, grouped,
            )
            if context_changed:
                delivery.recommendation_context = context
            body_changed = _redact_delivery_row(delivery, moment)
            _recount_impressions(db, delivery)
            request_ids.add(delivery.request_id)
            touched.add(delivery.delivery_id)
            if context_changed or body_changed:
                _log_batch(trace, "target_redaction", "recommendation_delivery", 1)
        _settle(db, commit)

    if touched:
        _fail_pending_outbox(db, sorted(touched))
    scrubbed = _scrub_request_facts(db, sorted(request_ids), grouped)
    _settle(db, commit)
    _log_batch(trace, "target_redaction", "recommendation_request", scrubbed)
    _log_batch(trace, "target_redaction", "recommendation_delivery", len(touched))
    return touched


# ---------------------------------------------------------------------------
# 步骤 5：conversation_log 反查 + Redis session
# ---------------------------------------------------------------------------

def redact_conversation_logs(
    db: Session,
    delivery_ids: Sequence[str],
    *,
    batch_id: str | None = None,
    commit: bool = False,
) -> int:
    """通过 ``conversation_log.recommendation_delivery_id`` 反查并覆盖为占位符。

    只写固定占位符，绝不回写任何原始 ``reply.content``（§9.11.1 步骤 6 行 2160）。
    """
    if not delivery_ids:
        return 0
    trace = batch_id or _new_batch_id()
    total = 0
    for chunk in _chunks(list(delivery_ids)):
        total += db.query(ConversationLog).filter(
            ConversationLog.recommendation_delivery_id.in_(chunk),
        ).update(
            {
                "content": REDACTED_PLACEHOLDER,
                "criteria_snapshot": None,
                "redaction_state": "redacted",
            },
            synchronize_session=False,
        )
        _settle(db, commit)
    _log_batch(trace, "conversation_redaction", "conversation_log", int(total or 0))
    return int(total or 0)


def _scrub_session_payload(
    session: dict,
    delivery_ids: set[str],
    grouped: dict[str, set[str]],
) -> bool:
    """就地擦掉一个 session 副本中的推荐痕迹，返回是否有变化。"""
    changed = False
    removable = _removable_ids(grouped, None)
    snapshot = session.get("candidate_snapshot")
    if isinstance(snapshot, dict):
        direction = snapshot.get("direction")
        snapshot_removable = (
            set(grouped.get("job", set()))
            if direction == "search_job"
            else set(grouped.get("resume", set()))
            if direction == "search_worker"
            else removable
        )
    else:
        snapshot_removable = removable

    history = session.get("history")
    if isinstance(history, list):
        new_history = []
        for entry in history:
            if not isinstance(entry, dict):
                new_history.append(entry)
                continue
            marker = str(entry.get("delivery_id") or "")
            content = entry.get("content")
            hits = marker in delivery_ids or (
                isinstance(content, str)
                and any(did in content for did in delivery_ids)
            )
            if not hits:
                new_history.append(entry)
                continue
            changed = True
            # 只留占位符，不保留任何候选元数据（§10.1.1 表格第三行）。
            new_history.append({
                "role": entry.get("role", "assistant"),
                "content": REDACTED_PLACEHOLDER,
            })
        if changed:
            session["history"] = new_history

    shown = session.get("shown_items")
    if isinstance(shown, list):
        kept = [
            item for item in shown
            if str(item) not in snapshot_removable
        ]
        if len(kept) != len(shown):
            session["shown_items"] = kept
            changed = True

    if isinstance(snapshot, dict):
        candidate_ids = snapshot.get("candidate_ids")
        if isinstance(candidate_ids, list):
            kept = [
                item for item in candidate_ids
                if str(item) not in snapshot_removable
            ]
            if len(kept) != len(candidate_ids):
                changed = True
                if kept:
                    snapshot["candidate_ids"] = kept
                else:
                    # 快照被清空后继续留着只会让 show_more 读到空壳。
                    session["candidate_snapshot"] = None
        if session.get("candidate_snapshot") is not None:
            metadata = snapshot.get("ranking_metadata")
            if isinstance(metadata, dict):
                score_map = metadata.get("candidate_scores")
                if isinstance(score_map, dict):
                    kept_scores = {
                        key: value for key, value in score_map.items()
                        if str(key) not in snapshot_removable
                    }
                    if len(kept_scores) != len(score_map):
                        metadata["candidate_scores"] = kept_scores
                        changed = True
                for list_key in (
                    "precision_pool_ids",
                    "shadow_top_ids",
                    "served_top_ids",
                ):
                    values = metadata.get(list_key)
                    if not isinstance(values, list):
                        continue
                    kept_values = [
                        value for value in values
                        if str(value) not in snapshot_removable
                    ]
                    if len(kept_values) != len(values):
                        metadata[list_key] = kept_values
                        changed = True
                if "shadow_rank_delta" in metadata:
                    scrubbed, hit = _strip_rank_delta(
                        metadata["shadow_rank_delta"],
                        snapshot_removable,
                    )
                    if hit:
                        metadata["shadow_rank_delta"] = scrubbed
                        changed = True
    return changed


def scrub_recommendation_sessions(
    delivery_ids: Sequence[str],
    targets: Sequence[TargetRef],
    *,
    owner_userid: str | None = None,
    batch_id: str | None = None,
) -> int:
    """按 Redis delivery/target session 索引重写受影响会话（§10.1.1 行 2237）。

    索引缺失时只能少清理，**不允许**退化成全库 ``KEYS`` 扫描（行 2228-2229）。
    Redis 不可用直接抛出，由调用方记失败并进重试队列。
    """
    from app.core import redis_client

    trace = batch_id or _new_batch_id()
    grouped = _target_map(targets)
    delivery_set = {str(d) for d in delivery_ids}
    index_keys = [f"{SESSION_DELIVERY_INDEX_PREFIX}{d}" for d in delivery_set]
    index_keys += [
        f"{SESSION_TARGET_INDEX_PREFIX}{kind}:{value}"
        for kind, ids in grouped.items()
        for value in ids
    ]
    if not index_keys:
        return 0

    r = redis_client.get_redis()
    userids: set[str] = set()
    for key in index_keys:
        try:
            members = r.smembers(key)
        except Exception:
            # 索引键可能被写入侧建成别的类型；单键失败不该拖垮整轮清理。
            logger.warning("recommendation_privacy: session index unreadable batch=%s", trace)
            continue
        for member in members or ():
            userids.add(member.decode() if isinstance(member, bytes) else str(member))
    if owner_userid:
        # 被删用户自己的 session 由 conversation_service.clear_session 处理，
        # 这里不重复写回，避免把已清空的会话又建出来。
        userids.discard(owner_userid)

    rewritten = 0
    for userid in sorted(userids):
        session = redis_client.get_session(userid)
        if not session:
            continue
        current_version = int(session.get("session_version") or 0)
        if not _scrub_session_payload(session, delivery_set, grouped):
            continue
        # 版本 +1：任何按旧版本算好的 staged mutation 会 CAS 失败，不会把刚擦掉的
        # 推荐正文再写回来。
        session["session_version"] = current_version + 1
        try:
            if redis_client.save_session_if_version(userid, session, current_version):
                rewritten += 1
        except Exception:
            logger.warning("recommendation_privacy: session rewrite failed batch=%s", trace)

    for key in index_keys:
        try:
            r.delete(key)
        except Exception:
            logger.warning("recommendation_privacy: session index delete failed batch=%s", trace)
    _log_batch(trace, "session_scrub", "redis_session", rewritten)
    return rewritten


# ---------------------------------------------------------------------------
# 步骤 2 / 3：事实行删除
# ---------------------------------------------------------------------------

def _delete_by_pk(
    db: Session,
    model,
    pk_column,
    filters: list,
    *,
    commit: bool,
) -> int:
    """分批删除，避免长事务（§9.11 行 2117）。"""
    total = 0
    while True:
        ids = [
            row[0]
            for row in db.query(pk_column).filter(*filters).limit(BATCH_SIZE).all()
        ]
        if not ids:
            break
        deleted = db.query(model).filter(pk_column.in_(ids)).delete(
            synchronize_session=False,
        )
        _settle(db, commit)
        total += int(deleted or 0)
        if len(ids) < BATCH_SIZE:
            break
    return total


def _delete_viewer_facts(
    db: Session,
    external_userid: str,
    report: PrivacyReport,
    *,
    commit: bool,
) -> None:
    """§9.11.1 步骤 2，严格按 §9.11 的外键顺序。"""
    request_ids = [
        str(row[0])
        for row in db.query(RecommendationRequest.request_id).filter(
            RecommendationRequest.viewer_userid == external_userid,
        ).all()
    ]

    # event_log.delivery_id → delivery 是 SET NULL，但这些事件本身属于被删用户，
    # 直接整行删。
    count = _delete_by_pk(
        db, EventLog, EventLog.id,
        [EventLog.userid == external_userid], commit=commit,
    )
    report.add("viewer_event_log", count)
    _log_batch(report.batch_id, "viewer_facts", "event_log", count)

    count = _delete_by_pk(
        db, RecommendationImpression, RecommendationImpression.id,
        [RecommendationImpression.viewer_userid == external_userid], commit=commit,
    )
    report.add("viewer_impression", count)
    _log_batch(report.batch_id, "viewer_facts", "recommendation_impression", count)

    # delivery 被删前先摘掉 outbox 对它的引用，避免 outbox 留下悬空 delivery_id。
    delivery_ids = [
        str(row[0])
        for row in db.query(RecommendationDelivery.delivery_id).filter(
            RecommendationDelivery.userid == external_userid,
        ).all()
    ]
    for chunk in _chunks(delivery_ids):
        db.query(WecomOutboundOutbox).filter(
            WecomOutboundOutbox.recommendation_delivery_id.in_(chunk),
        ).update(
            {"recommendation_delivery_id": None, "content": None},
            synchronize_session=False,
        )
    _settle(db, commit)

    count = _delete_by_pk(
        db, RecommendationDelivery, RecommendationDelivery.delivery_id,
        [RecommendationDelivery.userid == external_userid], commit=commit,
    )
    report.add("viewer_delivery", count)
    _log_batch(report.batch_id, "viewer_facts", "recommendation_delivery", count)

    if request_ids:
        # request.served_attempt_id → attempt 是 SET NULL；先摘引用才能删 attempt。
        for chunk in _chunks(request_ids):
            db.query(RecommendationRequest).filter(
                RecommendationRequest.request_id.in_(chunk),
            ).update({"served_attempt_id": None}, synchronize_session=False)
        _settle(db, commit)

        count = 0
        for chunk in _chunks(request_ids):
            count += _delete_by_pk(
                db, RecommendationSearchAttempt, RecommendationSearchAttempt.attempt_id,
                [RecommendationSearchAttempt.request_id.in_(chunk)], commit=commit,
            )
        report.add("viewer_attempt", count)
        _log_batch(report.batch_id, "viewer_facts", "recommendation_search_attempt", count)

        # request.parent_request_id → request 是 SET NULL，先摘掉子请求的引用。
        for chunk in _chunks(request_ids):
            db.query(RecommendationRequest).filter(
                RecommendationRequest.parent_request_id.in_(chunk),
            ).update({"parent_request_id": None}, synchronize_session=False)
        _settle(db, commit)

    count = _delete_by_pk(
        db, RecommendationRequest, RecommendationRequest.request_id,
        [RecommendationRequest.viewer_userid == external_userid], commit=commit,
    )
    report.add("viewer_request", count)
    _log_batch(report.batch_id, "viewer_facts", "recommendation_request", count)


def _delete_target_facts(
    db: Session,
    targets: Sequence[TargetRef],
    report: PrivacyReport,
    *,
    commit: bool,
) -> None:
    """§9.11.1 步骤 3 + 步骤 7：以被删用户候选为 target 的事实与日聚合。"""
    grouped = _target_map(targets)
    for target_type, ids in grouped.items():
        int_ids = [int(v) for v in ids]
        for chunk in _chunks(int_ids):
            count = _delete_by_pk(
                db, EventLog, EventLog.id,
                [EventLog.target_type == target_type, EventLog.target_id.in_(chunk)],
                commit=commit,
            )
            report.add("target_event_log", count)
            _log_batch(report.batch_id, "target_facts", "event_log", count)

            count = _delete_by_pk(
                db, RecommendationImpression, RecommendationImpression.id,
                [
                    RecommendationImpression.target_type == target_type,
                    RecommendationImpression.target_id.in_(chunk),
                ],
                commit=commit,
            )
            report.add("target_impression", count)
            _log_batch(report.batch_id, "target_facts", "recommendation_impression", count)

            # 步骤 7：日聚合的主键里带 target_id，属于「可回溯」聚合，必须整行删；
            # 不含 target 的纯计数聚合可以保留。
            count = db.query(RecommendationExposureDaily).filter(
                RecommendationExposureDaily.target_type == target_type,
                RecommendationExposureDaily.target_id.in_(chunk),
            ).delete(synchronize_session=False)
            _settle(db, commit)
            report.add("target_exposure_daily", int(count or 0))
            _log_batch(
                report.batch_id, "target_facts",
                "recommendation_exposure_daily", int(count or 0),
            )


# ---------------------------------------------------------------------------
# 步骤 6：本人日志删除与实体清理移交
# ---------------------------------------------------------------------------

def _delete_owned_content(
    db: Session,
    external_userid: str,
    targets: Sequence[TargetRef],
    report: PrivacyReport,
    *,
    now: datetime,
    commit: bool,
) -> None:
    """Delete logs and hand owned entities to the durable cleanup pipelines."""
    grouped = _target_map(targets)
    job_ids = sorted(int(value) for value in grouped.get("job", set()))
    for job_chunk in _chunks(job_ids):
        active_job = db.query(Job.id).filter(
            Job.id.in_(job_chunk),
            Job.owner_userid == external_userid,
            Job.deleted_at.is_(None),
        ).first()
        if active_job is not None:
            raise RuntimeError("active_job_requires_lifecycle_cleanup")

    count = _delete_by_pk(
        db, ConversationLog, ConversationLog.id,
        [ConversationLog.userid == external_userid], commit=commit,
    )
    report.add("conversation_log", count)
    _log_batch(report.batch_id, "owned_content", "conversation_log", count)

    from app.services.job_media_service import mark_entity_media_delete_pending
    from app.services.target_cleanup_service import ensure_job_cleanup_task

    deleted_at = to_naive_utc(now)
    for model, table in ((Job, "job"), (Resume, "resume")):
        target_ids = sorted(int(value) for value in grouped.get(table, set()))
        transitioned = 0
        for id_chunk in _chunks(target_ids):
            rows = db.query(model).populate_existing().filter(
                model.owner_userid == external_userid,
                model.id.in_(id_chunk),
            ).order_by(model.id).with_for_update().limit(BATCH_SIZE).all()
            for row in rows:
                changed = False
                if isinstance(row, Job):
                    if row.deleted_at is None:
                        raise RuntimeError("active_job_requires_lifecycle_cleanup")
                    ensure_job_cleanup_task(db, int(row.id), reason="manual_delete")
                elif row.deleted_at is None:
                    row.deleted_at = deleted_at
                    changed = True
                media_marked = mark_entity_media_delete_pending(db, table, int(row.id))
                changed = changed or bool(media_marked)
                transitioned += int(changed)
            _settle(db, commit)
        report.add(table, transitioned)
        _log_batch(report.batch_id, "owned_content", table, transitioned)


# ---------------------------------------------------------------------------
# 闭环入口
# ---------------------------------------------------------------------------

def delete_recommendation_user_data(
    db: Session,
    external_userid: str,
    *,
    now: datetime | None = None,
    commit: bool = True,
    batch_id: str | None = None,
    delete_owned_content: bool = True,
) -> PrivacyReport:
    """§9.11.1 的七步删除闭环。幂等；每一步失败都记进 ``report.failed_steps``。

    Args:
        commit: ``True``（延迟硬删任务）时逐批 commit 避免长事务；``False`` 时跟随
            调用方事务，只 flush。
        delete_owned_content: 关掉后跳过步骤 6 的 resume/job/conversation_log 删除，
            供「只清推荐域」的场景使用。
    """
    report = PrivacyReport(batch_id=batch_id or _new_batch_id())
    moment = now or utc_now()

    def _step(name: str, fn) -> Any:
        try:
            return fn()
        except Exception as exc:
            # 只记异常类名：SQLAlchemy 的异常消息会把 SQL 和绑定参数（含 userid、
            # target ID）一起带出来，而 §14.12 行 3400 要求删除日志里不许出现它们。
            logger.error(
                "recommendation_privacy: step failed batch=%s step=%s error=%s",
                report.batch_id, name, type(exc).__name__,
            )
            report.failed_steps.append(name)
            if commit:
                db.rollback()
            return None

    # 步骤 1：候选 target 清单。
    targets: list[TargetRef] = _step(
        "collect_targets", lambda: owned_target_refs(db, external_userid),
    ) or []
    report.targets = len(targets)

    # 无论后续哪一步失败，正文都必须先没：§10.1.1 行 2240「不得继续保留可解密正文」。
    _step("redact_own_content", lambda: redact_user_recommendation_content(
        db, external_userid, now=moment, batch_id=report.batch_id, commit=commit,
    ))

    # 步骤 4：先清别人 delivery 的正文与候选引用，并拿到反查用的 delivery ID。
    touched_deliveries = _step(
        "redact_target_deliveries",
        lambda: redact_deliveries_for_targets(
            db, targets, now=moment, batch_id=report.batch_id,
            commit=commit, exclude_userid=external_userid,
        ),
    ) or set()
    report.redacted_deliveries = len(touched_deliveries)

    # 步骤 5：conversation_log 反查覆盖 + Redis session 重写。
    _step("redact_conversation_logs", lambda: redact_conversation_logs(
        db, sorted(touched_deliveries), batch_id=report.batch_id, commit=commit,
    ))
    # 被删用户自己的 delivery 也要进 session 索引清单：他的 session 已在命令阶段
    # 清空，但索引键本身还在 Redis 里，必须一起删掉，否则残留可反查的 delivery ID。
    own_delivery_ids = _step("collect_own_deliveries", lambda: [
        str(row[0])
        for row in db.query(RecommendationDelivery.delivery_id).filter(
            RecommendationDelivery.userid == external_userid,
        ).all()
    ]) or []
    _step("scrub_sessions", lambda: scrub_recommendation_sessions(
        sorted(touched_deliveries | set(own_delivery_ids)), targets,
        owner_userid=external_userid, batch_id=report.batch_id,
    ))

    # 步骤 2：viewer 侧事实，按外键顺序。
    _step("delete_viewer_facts", lambda: _delete_viewer_facts(
        db, external_userid, report, commit=commit,
    ))

    # 步骤 3 + 7：候选侧事实与可回溯日聚合。
    _step("delete_target_facts", lambda: _delete_target_facts(
        db, targets, report, commit=commit,
    ))

    # impression 行删干净之后才有终值，回到步骤 4 把 expected/actual 对齐。
    _step("recount_impressions", lambda: recount_delivery_impressions(
        db, sorted(touched_deliveries), commit=commit,
    ))

    # 步骤 6：本人日志删除，并把原始 target 快照移交 durable 清理。
    if delete_owned_content and report.ok:
        _step("delete_owned_content", lambda: _delete_owned_content(
            db, external_userid, targets, report, now=moment, commit=commit,
        ))

    logger.info(
        "recommendation_privacy: closure batch=%s ok=%s rows=%d failed=%s",
        report.batch_id, report.ok, report.total_rows, ",".join(report.failed_steps),
    )
    return report
