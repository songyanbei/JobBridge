"""Transactional recommendation delivery, envelope encryption and content TTL.

Recommendation bodies are intentionally kept out of the outbox and the
conversation log (§10.1.1).  The only durable copy is the versioned AES-GCM
envelope written here, and it lives exactly as long as §9.11 allows.
"""
from __future__ import annotations

import base64
import hashlib
import os
import struct
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, get_args

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy.orm import Session

from app.config import settings
from app.core.time_utils import ensure_utc, to_naive_utc, utc_now
from app.models import (
    RecommendationDelivery,
    RecommendationRequest,
    RecommendationSearchAttempt,
    Resume,
    WecomInboundEvent,
    WecomOutboundOutbox,
)
from app.schemas.recommendation import (
    ATTEMPT_KIND_BY_REQUEST_KIND,
    AttemptKind,
    LlmAttemptStatus,
    project_delivery_context,
)
from app.wecom.aibot_client import stable_aibot_stream_id

# ---------------------------------------------------------------------------
# §9.6 / §9.11 版本化信封加密
# ---------------------------------------------------------------------------
# envelope = MAGIC || key_version(BE16) || nonce(12) || ciphertext || auth_tag
# 整体再做一次 urlsafe base64，这样它既能直接进 MEDIUMBLOB，也能被历史上按
# ascii 取用的调用方安全 decode。key version 写进信封本身而不是只写
# ``content_key_version`` 列，是因为 ``session_patch_ciphertext`` 没有自己的
# 版本列，轮换期间必须能按行判断该用哪把 key。版本号同时进 AAD，改不了。

_ENVELOPE_MAGIC = b"rce1"
_KEY_VERSION_BYTES = 2
_NONCE_BYTES = 12
_HEADER_BYTES = len(_ENVELOPE_MAGIC) + _KEY_VERSION_BYTES

#: AAD 的用途维度：正文和 session patch 用同一把 key，但不能互相替换。
CONTENT_PURPOSE = "content"
SESSION_PATCH_PURPOSE = "session_patch"
_ALLOWED_PURPOSES = frozenset({CONTENT_PURPOSE, SESSION_PATCH_PURPOSE})

_AAD_DOMAIN = "jobbridge:recommendation-delivery:v1"

# ---------------------------------------------------------------------------
# §9.11 正文保留
# ---------------------------------------------------------------------------
#: sent / permanent_failed 之后正文最多再留 24 小时。
CONTENT_TTL_TERMINAL_HOURS = 24
#: unknown（企微结果不可判定）最多保留 7 天。
CONTENT_TTL_UNKNOWN_DAYS = 7
#: prepared 超过 24 小时仍无法提交 session，转 permanent_failed 并清空正文。
PREPARED_COMMIT_DEADLINE_HOURS = 24

_TERMINAL_CONTENT_STATUSES = frozenset({"sent", "permanent_failed"})
#: 仍可能需要正文的在途状态；上界取和 unknown 一样的 7 天，避免卡住的行永久留明文可解密副本。
_IN_FLIGHT_CONTENT_STATUSES = frozenset({"pending", "sending", "retry_wait"})


class ContentEnvelopeError(RuntimeError):
    """§10.6：正文加解密失败一律 fail-closed，禁止明文旁路。"""


def active_content_key_version() -> int:
    """新写入 ciphertext 使用的 key 版本，会落到 ``content_key_version`` 列。"""
    return int(settings.recommendation_content_key_active_version)


def _aead(key_version: int) -> AESGCM:
    """按版本从只读 key ring 取 key。

    没有任何硬编码兜底：缺 key 时 ``recommendation_content_key_material`` 抛错，
    调用方按 §10.6 fail-closed，而不是用固定常量“保护”用户正文。
    """
    material = settings.recommendation_content_key_material(key_version)
    return AESGCM(hashlib.sha256(material.encode("utf-8")).digest())


def _aad(*, delivery_id: str, userid: str, purpose: str, key_version: int) -> bytes:
    """AAD 至少绑定 delivery_id / userid / purpose（§9.6），防跨行跨用途替换。"""
    if purpose not in _ALLOWED_PURPOSES:
        raise ContentEnvelopeError(f"unknown recommendation envelope purpose: {purpose}")
    if not delivery_id or not userid:
        raise ContentEnvelopeError(
            "recommendation envelope requires delivery_id and userid for AAD binding"
        )
    return "|".join((
        _AAD_DOMAIN, str(delivery_id), str(userid), purpose, str(int(key_version)),
    )).encode("utf-8")


def _parse_envelope(envelope: bytes | str) -> tuple[int, bytes, bytes]:
    raw = envelope.encode("ascii") if isinstance(envelope, str) else bytes(envelope)
    try:
        blob = base64.urlsafe_b64decode(raw)
    except Exception as exc:  # noqa: BLE001 - 任何解码异常都等价于信封不可用
        raise ContentEnvelopeError("recommendation envelope is not valid base64") from exc
    if not blob.startswith(_ENVELOPE_MAGIC) or len(blob) <= _HEADER_BYTES + _NONCE_BYTES:
        # 迁移前用「sha256(单个 env) + AAD=None」写的旧行没有信封头，对应的 key
        # 已经退役，只能 fail-closed 让 TTL 清理掉，不能猜测密钥。
        raise ContentEnvelopeError("recommendation envelope header is missing or truncated")
    key_version = struct.unpack(
        ">H", blob[len(_ENVELOPE_MAGIC):_HEADER_BYTES],
    )[0]
    nonce = blob[_HEADER_BYTES:_HEADER_BYTES + _NONCE_BYTES]
    return int(key_version), nonce, blob[_HEADER_BYTES + _NONCE_BYTES:]


def encrypt_delivery_field(
    plaintext: str,
    *,
    delivery_id: str,
    userid: str,
    purpose: str,
    key_version: int | None = None,
) -> bytes:
    version = int(key_version) if key_version is not None else active_content_key_version()
    aad = _aad(
        delivery_id=delivery_id, userid=userid, purpose=purpose, key_version=version,
    )
    # key ring 缺版本时让 config 的 RuntimeError 原样冒出来，运维需要看到是哪一版没配。
    aead = _aead(version)
    try:
        nonce = os.urandom(_NONCE_BYTES)
        sealed = aead.encrypt(nonce, plaintext.encode("utf-8"), aad)
    except Exception as exc:  # noqa: BLE001
        raise ContentEnvelopeError("recommendation content encryption failed") from exc
    header = _ENVELOPE_MAGIC + struct.pack(">H", version)
    return base64.urlsafe_b64encode(header + nonce + sealed)


def decrypt_delivery_field(
    envelope: bytes | str,
    *,
    delivery_id: str,
    userid: str,
    purpose: str,
) -> str:
    key_version, nonce, sealed = _parse_envelope(envelope)
    aad = _aad(
        delivery_id=delivery_id, userid=userid, purpose=purpose, key_version=key_version,
    )
    # 旧 key 被过早退役同样按 config 的 RuntimeError 冒出来，而不是伪装成密文损坏。
    aead = _aead(key_version)
    try:
        return aead.decrypt(nonce, sealed, aad).decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - InvalidTag / UnicodeDecodeError 一视同仁
        raise ContentEnvelopeError("recommendation content decryption failed") from exc


def encrypt_body(
    body: str, *, delivery_id: str, userid: str, key_version: int | None = None,
) -> bytes:
    return encrypt_delivery_field(
        body, delivery_id=delivery_id, userid=userid,
        purpose=CONTENT_PURPOSE, key_version=key_version,
    )


def decrypt_body(envelope: bytes | str, *, delivery_id: str, userid: str) -> str:
    return decrypt_delivery_field(
        envelope, delivery_id=delivery_id, userid=userid, purpose=CONTENT_PURPOSE,
    )


def encrypt_session_patch(
    payload: str, *, delivery_id: str, userid: str, key_version: int | None = None,
) -> bytes:
    return encrypt_delivery_field(
        payload, delivery_id=delivery_id, userid=userid,
        purpose=SESSION_PATCH_PURPOSE, key_version=key_version,
    )


def decrypt_session_patch(envelope: bytes | str, *, delivery_id: str, userid: str) -> str:
    return decrypt_delivery_field(
        envelope, delivery_id=delivery_id, userid=userid, purpose=SESSION_PATCH_PURPOSE,
    )


def decrypt_delivery_body(delivery: RecommendationDelivery) -> str:
    """按行解密正文；AAD 取自这一行自己的 delivery_id/userid。"""
    if not delivery.content_ciphertext:
        raise ContentEnvelopeError("recommendation delivery has no content ciphertext")
    return decrypt_body(
        delivery.content_ciphertext,
        delivery_id=delivery.delivery_id,
        userid=delivery.userid,
    )


def decrypt_delivery_session_patch(delivery: RecommendationDelivery) -> str | None:
    if not delivery.session_patch_ciphertext:
        return None
    return decrypt_session_patch(
        delivery.session_patch_ciphertext,
        delivery_id=delivery.delivery_id,
        userid=delivery.userid,
    )


def store_session_patch(delivery: RecommendationDelivery, payload: str) -> None:
    """prepared 阶段暂存 Redis 恢复 patch；进入 pending 后必须立即清空。"""
    delivery.session_patch_ciphertext = encrypt_session_patch(
        payload, delivery_id=delivery.delivery_id, userid=delivery.userid,
    )


def clear_session_patch(delivery: RecommendationDelivery) -> None:
    """§9.11：prepared→pending 时立即清空 session patch，不进入 90 天留存。"""
    delivery.session_patch_ciphertext = None


def content_digest(body: str, *, delivery_id: str) -> str:
    """§10.1.1 的不可逆 ``content_hash``。

    按 delivery 加盐，使它只能用于“这一行的正文有没有被改过”的校验，不能拿来跨行、
    跨用户比对推荐正文是否相同。
    """
    material = f"jobbridge:recommendation-content:v1|{delivery_id}|{body}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# §9.11 content_expires_at
# ---------------------------------------------------------------------------

def content_expires_at_for_status(
    status: str,
    *,
    created_at: datetime | None,
    terminal_at: datetime | None = None,
) -> datetime:
    """按 delivery 状态算正文清理时间（aware UTC）。

    §9.11 的三条规则：

    * ``sent`` / ``permanent_failed``：终态时刻 + 24 小时；
    * ``unknown``：自创建起最多 7 天；
    * ``prepared``：创建后 24 小时就是 session 提交死线，到点转 permanent_failed
      并同时清空正文，所以 TTL 直接等于这个死线。

    其余在途状态（pending/sending/retry_wait）方案没有单独给上界，这里取和
    unknown 同样的 7 天：卡住的行不该无限期留着可解密正文。

    ``worker``/``ttl`` 任务应当调用本函数或 ``apply_content_ttl``，不要各自写死分钟数。
    """
    created = ensure_utc(created_at) or utc_now()
    normalized = (status or "").strip().lower()
    if normalized in _TERMINAL_CONTENT_STATUSES:
        return (ensure_utc(terminal_at) or utc_now()) + timedelta(
            hours=CONTENT_TTL_TERMINAL_HOURS,
        )
    if normalized == "prepared":
        return created + timedelta(hours=PREPARED_COMMIT_DEADLINE_HOURS)
    if normalized == "unknown" or normalized in _IN_FLIGHT_CONTENT_STATUSES:
        return created + timedelta(days=CONTENT_TTL_UNKNOWN_DAYS)
    # 未知状态按最严格的一档处理，宁可早清也不留可解密正文。
    return created + timedelta(hours=CONTENT_TTL_TERMINAL_HOURS)


def apply_content_ttl(
    delivery: RecommendationDelivery,
    *,
    status: str | None = None,
    terminal_at: datetime | None = None,
) -> datetime:
    """把按状态算出的 TTL 写回 ``content_expires_at``（naive UTC 列）。"""
    expires_at = content_expires_at_for_status(
        status or delivery.status or "prepared",
        created_at=delivery.created_at,
        terminal_at=terminal_at,
    )
    delivery.content_expires_at = to_naive_utc(expires_at)
    return expires_at


def prepared_commit_deadline(delivery: RecommendationDelivery) -> datetime:
    created = ensure_utc(delivery.created_at) or utc_now()
    return created + timedelta(hours=PREPARED_COMMIT_DEADLINE_HOURS)


def purge_delivery_content(delivery: RecommendationDelivery) -> bool:
    """清空两个 ciphertext 并把 ``content_expires_at`` 置当前时刻（§10.1.1 第 1 步）。"""
    had_content = bool(delivery.content_ciphertext or delivery.session_patch_ciphertext)
    delivery.content_ciphertext = None
    delivery.session_patch_ciphertext = None
    delivery.content_expires_at = to_naive_utc(utc_now())
    return had_content


def expire_prepared_delivery(
    delivery: RecommendationDelivery, *, now: datetime | None = None,
) -> bool:
    """prepared 超过 24 小时仍未提交 session → permanent_failed + 清空正文（§9.11）。"""
    if delivery.status != "prepared":
        return False
    moment = ensure_utc(now) or utc_now()
    if moment < prepared_commit_deadline(delivery):
        return False
    delivery.status = "permanent_failed"
    delivery.last_error_code = delivery.last_error_code or "session_commit_deadline"
    delivery.last_error = (
        delivery.last_error
        or "prepared delivery exceeded the 24h session commit deadline"
    )[:500]
    purge_delivery_content(delivery)
    return True


# ---------------------------------------------------------------------------
# §9.4 / §9.5 / §9.6 事实持久化
# ---------------------------------------------------------------------------

_EXECUTION_MODES = frozenset({"off", "shadow", "on"})
_ASSIGNMENTS = frozenset({"legacy", "stable", "candidate"})
_ATTEMPT_KINDS = frozenset(get_args(AttemptKind))
_LLM_STATUSES = frozenset(get_args(LlmAttemptStatus))
_MAX_CANDIDATE_IDS = 50


class RecommendationTargetStale(RuntimeError):
    """Stable fail-closed result for a target invalidated after search."""

    code = "recommendation_target_stale"

    def __init__(self) -> None:
        super().__init__(self.code)


def _resume_target_ids(
    ctx: Mapping[str, Any], fact: Mapping[str, Any],
) -> list[int]:
    """Return every Resume id that the transaction is about to persist."""
    ids: set[int] = set()
    for item in ctx.get("items") or []:
        if not isinstance(item, Mapping) or item.get("target_type") != "resume":
            continue
        value = _optional_int(item.get("target_id"))
        if value is not None and value > 0:
            ids.add(value)
    if str(fact.get("direction") or ctx.get("direction") or "") == "search_worker":
        served_top_ids = _persisted_id_strings(fact.get("served_top_ids") or [])
        candidate_ids, precision_pool_ids = _attempt_persisted_id_lists(
            fact, candidate_fallback=served_top_ids,
        )
        collections = [
            served_top_ids,
            candidate_ids,
            precision_pool_ids,
            _persisted_id_strings(fact.get("shadow_top_ids") or []),
        ]
        for raw_attempt in fact.get("additional_attempts") or []:
            if isinstance(raw_attempt, Mapping):
                collections.extend(_attempt_persisted_id_lists(raw_attempt))
        for values in collections:
            for raw in values:
                value = _optional_int(raw)
                if value is not None and value > 0:
                    ids.add(value)
    return sorted(ids)


def lock_and_validate_recommendation_targets(
    db: Session,
    *,
    ctx: Mapping[str, Any],
    fact: Mapping[str, Any],
    now: datetime,
) -> list[int]:
    """Linearize Resume recommendation persistence against delist/expiry.

    Lock ordering is the canonical ascending Resume id order.  The caller must
    invoke this before writing request, attempt, delivery, outbox or a durable
    session patch and retain the locks until its transaction commits.
    """
    target_ids = _resume_target_ids(ctx, fact)
    if not target_ids:
        return []
    moment = to_naive_utc(now)
    rows = (
        db.query(Resume)
        .populate_existing()
        .filter(Resume.id.in_(target_ids))
        .order_by(Resume.id)
        .with_for_update()
        .all()
    )
    if [int(row.id) for row in rows] != target_ids or any(
        row.audit_status != "passed"
        or row.activated_at is None
        or row.candidate_expires_at is not None
        or row.deleted_at is not None
        or row.delist_reason is not None
        or row.expires_at is None
        or row.expires_at <= moment
        for row in rows
    ):
        raise RecommendationTargetStale()
    return target_ids


def _enum(value: Any, allowed: frozenset[str], default: str) -> str:
    text = str(value or "").strip()
    return text if text in allowed else default


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, str) and value:
        try:
            return ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _persisted_id_strings(values: Any, *, limit: int | None = None) -> list[str]:
    """Normalize an ID collection exactly once for both locking and storage."""
    normalized = [str(value) for value in (values or [])]
    return normalized if limit is None else normalized[:limit]


def _attempt_persisted_id_lists(
    attempt: Mapping[str, Any], *, candidate_fallback: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Return the two Resume-bearing collections persisted for an attempt."""
    candidate_ids = _persisted_id_strings(
        attempt.get("candidate_ids") or candidate_fallback or [],
        limit=_MAX_CANDIDATE_IDS,
    )
    precision_pool_ids = _persisted_id_strings(
        attempt.get("precision_pool_ids") or [],
        limit=_MAX_CANDIDATE_IDS,
    )
    return candidate_ids, precision_pool_ids


def _criteria_digest(
    fact: Mapping[str, Any], *, direction: str, query_digest: str, algorithm_version: str,
) -> str:
    """§9.5 的 CHAR(64) 条件摘要。

    上游只给 16 位 ``query_digest`` 时不能把短摘要塞进宽列冒充，改用固定域分隔的
    SHA-256 补成真正的 64 位摘要——同一组有效条件仍然得到同一个值。
    """
    raw = str(fact.get("criteria_digest") or "").strip().lower()
    if len(raw) == 64 and all(char in "0123456789abcdef" for char in raw):
        return raw
    material = "|".join((
        "jobbridge:recommendation-criteria:v1", direction, query_digest, algorithm_version,
    ))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _owner_stats(items: list[Mapping[str, Any]]) -> tuple[int, int, int]:
    """从**内存态**上下文统计 owner 集中度和探索位数量。

    owner_userid 只在这里参与计算，不进 ``recommendation_context``（§9.6 白名单）。
    """
    per_owner: dict[str, int] = {}
    exploration = 0
    for item in items:
        owner = item.get("owner_userid")
        if owner:
            per_owner[str(owner)] = per_owner.get(str(owner), 0) + 1
        if item.get("is_exploration"):
            exploration += 1
    return len(per_owner), (max(per_owner.values()) if per_owner else 0), exploration


def _item_keys(context: Mapping[str, Any]) -> set[tuple[str, int]]:
    """能真正变成 impression 的候选（§9.6 ``impression_expected_count`` 的口径）。"""
    keys: set[tuple[str, int]] = set()
    for item in context.get("items") or []:
        target_type = item.get("target_type")
        target_id = _optional_int(item.get("target_id"))
        if isinstance(target_type, str) and target_type and target_id is not None:
            keys.add((target_type, target_id))
    return keys


def _persist_request_facts(
    db: Session,
    *,
    request_id: str,
    source_inbound_msg_id: str,
    request_index: int,
    userid: str,
    snapshot_id: str | None,
    ctx: Mapping[str, Any],
    fact: Mapping[str, Any],
    items: list[Mapping[str, Any]],
    now: datetime,
) -> None:
    """同一事务内写 request + attempt，并回填 ``served_attempt_id``。

    ``request.served_attempt_id`` 与 ``attempt.request_id`` 互相引用（§9.11 外键
    合同），MySQL 没有延迟外键，所以顺序固定为：先插 request 且 served_attempt_id
    为 NULL → 插 attempt → UPDATE 回填。提交前必须非 NULL，否则整笔事务回滚。
    """
    if db.get(RecommendationRequest, request_id) is not None:
        return

    request_kind = _enum(
        fact.get("request_kind"), frozenset(ATTEMPT_KIND_BY_REQUEST_KIND), "initial_search",
    )
    parent_request_id = fact.get("parent_request_id") or None
    parent = db.get(RecommendationRequest, parent_request_id) if parent_request_id else None
    # 父行不存在时不能带着悬空 ID 写库，否则直接违反 fk_recommendation_request_parent。
    parent_request_id = parent.request_id if parent is not None else None

    served_assignment = _enum(
        fact.get("served_assignment") or ctx.get("assignment"), _ASSIGNMENTS, "legacy",
    )
    execution_mode = _enum(fact.get("execution_mode"), _EXECUTION_MODES, "off")
    if execution_mode == "off" and served_assignment != "legacy":
        # §7：shadow 从不改变 served assignment，off 更不会。真的服务了
        # stable/candidate 就只可能是 on 模式，上游没传值时按这个反推。
        execution_mode = "on"
    algorithm_version = str(
        fact.get("algorithm_version")
        or ctx.get("algorithm_version")
        or ("legacy" if served_assignment == "legacy" else "recommendation-v1")
    )[:32]
    direction = str(fact.get("direction") or ctx.get("direction") or "search_job")[:32]
    query_digest = str(fact.get("query_digest") or ctx.get("query_digest") or "")[:16]
    strategy_version_id = _optional_int(
        fact.get("served_strategy_version_id")
        if fact.get("served_strategy_version_id") is not None
        else ctx.get("strategy_version_id")
    )

    served_top_ids = _persisted_id_strings(fact.get("served_top_ids") or [])
    if not served_top_ids:
        served_top_ids = [
            str(item.get("target_id")) for item in items if item.get("target_id") is not None
        ]
    owner_count, max_owner_items, exploration_count = _owner_stats(items)

    # §9.4：show_more 的零结果语义是"分页耗尽"，不得污染业务零结果率。
    is_show_more = request_kind == "show_more"
    is_zero_result = (
        False if is_show_more
        else bool(fact.get("is_zero_result")) or not served_top_ids
    )
    show_more_exhausted = (
        bool(fact.get("show_more_exhausted", not served_top_ids)) if is_show_more else False
    )

    request = RecommendationRequest(
        request_id=request_id,
        source_inbound_msg_id=source_inbound_msg_id,
        request_index=request_index,
        request_kind=request_kind,
        parent_request_id=parent_request_id,
        served_attempt_id=None,
        snapshot_id=snapshot_id,
        viewer_userid=userid,
        direction=direction,
        query_digest=query_digest,
        execution_mode=execution_mode,
        served_assignment=served_assignment,
        served_strategy_version_id=strategy_version_id,
        candidate_strategy_version_id=_optional_int(fact.get("candidate_strategy_version_id")),
        algorithm_version=algorithm_version,
        final_candidate_count=int(fact.get("candidate_count", len(served_top_ids)) or 0),
        result_count=int(fact.get("result_count", len(items)) or len(items)),
        is_zero_result=is_zero_result,
        show_more_exhausted=show_more_exhausted,
        total_latency_ms=int(fact.get("total_latency_ms", 0) or 0),
        served_top_ids=served_top_ids,
        served_owner_count=int(fact.get("served_owner_count", owner_count) or owner_count),
        served_max_owner_items=int(
            fact.get("served_max_owner_items", max_owner_items) or max_owner_items,
        ),
        served_exploration_count=int(
            fact.get("served_exploration_count", exploration_count) or exploration_count,
        ),
    )
    db.add(request)
    db.flush()

    # §9.4：show_more 复用创建快照那次 request 的 served attempt，不建新候选池。
    served_attempt_id = parent.served_attempt_id if is_show_more and parent else None
    if served_attempt_id is None:
        candidate_ids, precision_pool_ids = _attempt_persisted_id_lists(
            fact, candidate_fallback=served_top_ids,
        )
        attempt_kind = _enum(
            fact.get("attempt_kind"),
            _ATTEMPT_KINDS,
            ATTEMPT_KIND_BY_REQUEST_KIND.get(request_kind, "initial"),
        )
        attempt_id = str(uuid.uuid4())
        db.add(RecommendationSearchAttempt(
            attempt_id=attempt_id,
            request_id=request_id,
            attempt_no=int(fact.get("attempt_no", 0) or 0),
            attempt_kind=attempt_kind,
            criteria_digest=_criteria_digest(
                fact,
                direction=direction,
                query_digest=query_digest,
                algorithm_version=algorithm_version,
            ),
            # 打分时刻，不是落库时刻：缺省只能退回 now，但上游应当传真值。
            scoring_time_utc=to_naive_utc(_as_datetime(fact.get("scoring_time_utc")) or now),
            candidate_count=int(fact.get("candidate_count", len(candidate_ids)) or 0),
            candidate_ids=candidate_ids,
            precision_pool_ids=precision_pool_ids,
            result_count=len(items),
            is_zero_result=not candidate_ids,
            strategy_version_id=strategy_version_id,
            algorithm_version=algorithm_version,
            llm_status=_enum(fact.get("llm_status"), _LLM_STATUSES, "skipped"),
            llm_input_tokens=_optional_int(fact.get("llm_input_tokens")),
            llm_output_tokens=_optional_int(fact.get("llm_output_tokens")),
            llm_timeout_budget_ms=_optional_int(fact.get("llm_timeout_budget_ms")),
            llm_retry_count=int(fact.get("llm_retry_count", 0) or 0),
            ranking_fallback=(str(fact["ranking_fallback"])[:32]
                              if fact.get("ranking_fallback") else None),
            ranking_latency_ms=int(fact.get("ranking_latency_ms", 0) or 0),
            total_latency_ms=int(
                fact.get("attempt_latency_ms", fact.get("total_latency_ms", 0)) or 0,
            ),
        ))
        db.flush()
        served_attempt_id = attempt_id

        # §9.5：一次 request 可能实际执行 initial、多个 relax_probe，最后再
        # auto_relaxed。served_attempt_id 只指向最终服务用户的 attempt，但每条
        # 真查询都必须落一行，不能只在 request 上留步骤名。
        for raw_attempt in fact.get("additional_attempts") or []:
            if not isinstance(raw_attempt, Mapping):
                continue
            extra = dict(raw_attempt)
            extra_candidate_ids, extra_precision_pool_ids = (
                _attempt_persisted_id_lists(extra)
            )
            extra_kind = _enum(
                extra.get("attempt_kind"),
                _ATTEMPT_KINDS,
                "relax_probe",
            )
            db.add(RecommendationSearchAttempt(
                attempt_id=str(uuid.uuid4()),
                request_id=request_id,
                attempt_no=max(0, int(extra.get("attempt_no", 0) or 0)),
                attempt_kind=extra_kind,
                criteria_digest=_criteria_digest(
                    extra,
                    direction=direction,
                    query_digest=query_digest,
                    algorithm_version=algorithm_version,
                ),
                scoring_time_utc=to_naive_utc(
                    _as_datetime(extra.get("scoring_time_utc")) or now,
                ),
                candidate_count=int(
                    extra.get("candidate_count", len(extra_candidate_ids)) or 0,
                ),
                candidate_ids=extra_candidate_ids,
                precision_pool_ids=extra_precision_pool_ids,
                result_count=int(
                    extra.get("result_count", len(extra_candidate_ids)) or 0,
                ),
                is_zero_result=bool(
                    extra.get("is_zero_result", not extra_candidate_ids),
                ),
                strategy_version_id=strategy_version_id,
                algorithm_version=algorithm_version,
                llm_status=_enum(
                    extra.get("llm_status"), _LLM_STATUSES, "skipped",
                ),
                llm_input_tokens=_optional_int(extra.get("llm_input_tokens")),
                llm_output_tokens=_optional_int(extra.get("llm_output_tokens")),
                llm_timeout_budget_ms=_optional_int(
                    extra.get("llm_timeout_budget_ms"),
                ),
                llm_retry_count=int(extra.get("llm_retry_count", 0) or 0),
                ranking_fallback=(
                    str(extra["ranking_fallback"])[:32]
                    if extra.get("ranking_fallback") else None
                ),
                ranking_latency_ms=int(
                    extra.get("ranking_latency_ms", 0) or 0,
                ),
                total_latency_ms=int(
                    extra.get(
                        "attempt_latency_ms",
                        extra.get("ranking_latency_ms", 0),
                    ) or 0,
                ),
            ))
        db.flush()

    request.served_attempt_id = served_attempt_id
    db.flush()


def prepare_delivery(
    db: Session,
    *,
    inbound_event_id: int,
    reply_index: int,
    userid: str,
    body: str,
    request_id: str | None = None,
    snapshot_id: str | None = None,
    position_count: int = 0,
    delivery_id: str | None = None,
    recommendation_context: dict | None = None,
    source_inbound_msg_id: str | None = None,
    request_fact: dict | None = None,
    now: datetime | None = None,
) -> RecommendationDelivery:
    """在调用方事务内写 request/attempt/delivery/outbox（§10.2 第 5 步）。

    任何一步失败都必须让整笔事务回滚：§10.6 要求事实写不下去就不发这条推荐。
    ``position_count`` 只在上下文没有 items 时用作候选数兜底。
    """
    ctx = dict(recommendation_context or {})
    fact = dict(request_fact or {})
    items = [item for item in (ctx.get("items") or []) if isinstance(item, Mapping)]
    moment = ensure_utc(now) or utc_now()
    naive_moment = to_naive_utc(moment)

    request_id = request_id or str(fact.get("request_id") or "") or str(uuid.uuid4())
    resolved_delivery_id = delivery_id or str(ctx.get("delivery_id") or "") or str(uuid.uuid4())
    # 空串会被当成合法快照 ID 写进 CHAR(36) 列，反查时永远匹配不上。
    resolved_snapshot_id = (snapshot_id or fact.get("snapshot_id") or ctx.get("snapshot_id")) or None
    resolved_inbound_msg_id = str(
        source_inbound_msg_id or fact.get("source_inbound_msg_id") or inbound_event_id,
    )[:64]
    # §9.4：同一入站消息的第 N 个推荐决策必须有不同的 request_index，否则撞唯一键。
    request_index = fact.get("request_index")
    request_index = reply_index if request_index is None else int(request_index)

    # The inbound event is the source of truth for the delivery channel and
    # conversation target.  Recommendation delivery used to create a bare
    # legacy outbox row here, which made an AIBot search reply default to
    # ``wecom_app`` and violate the outbox conversation contract.
    inbound = db.get(WecomInboundEvent, int(inbound_event_id))
    source_channel = getattr(inbound, "source_channel", None) or "wecom_app"
    conversation_type = getattr(inbound, "conversation_type", None) or "single"
    conversation_id = getattr(inbound, "conversation_id", None)
    chat_id = getattr(inbound, "chat_id", None)
    ordering_key = getattr(inbound, "ordering_key", None)
    provider_req_id = getattr(inbound, "provider_req_id", None)
    aibot_reply = source_channel == "wecom_aibot"
    outbox_stream_id = stable_aibot_stream_id(inbound_event_id, reply_index) if aibot_reply else None
    outbox_finish = True if aibot_reply else None
    outbox_reply_expires_at = (
        (getattr(inbound, "created_at", None) or naive_moment) + timedelta(hours=24)
        if aibot_reply else None
    )

    lock_and_validate_recommendation_targets(
        db, ctx=ctx, fact=fact, now=moment,
    )

    _persist_request_facts(
        db,
        request_id=request_id,
        source_inbound_msg_id=resolved_inbound_msg_id,
        request_index=request_index,
        userid=userid,
        snapshot_id=resolved_snapshot_id,
        ctx=ctx,
        fact=fact,
        items=items,
        now=moment,
    )

    persisted_context = project_delivery_context(ctx)
    expected_count = len(_item_keys(persisted_context)) or max(0, int(position_count or 0))
    key_version = active_content_key_version()
    delivery = RecommendationDelivery(
        delivery_id=resolved_delivery_id,
        source_inbound_msg_id=resolved_inbound_msg_id,
        reply_index=reply_index,
        request_id=request_id,
        snapshot_id=resolved_snapshot_id,
        userid=userid,
        content_ciphertext=encrypt_body(
            body, delivery_id=resolved_delivery_id, userid=userid, key_version=key_version,
        ),
        content_key_version=key_version,
        content_hash=content_digest(body, delivery_id=resolved_delivery_id),
        content_expires_at=to_naive_utc(
            content_expires_at_for_status("prepared", created_at=moment),
        ),
        recommendation_context=persisted_context,
        session_commit_token=resolved_delivery_id,
        session_commit_state="not_applied",
        next_attempt_at=naive_moment,
        impression_next_attempt_at=naive_moment,
        impression_expected_count=expected_count,
        created_at=naive_moment,
        status="prepared",
    )
    db.add(delivery)
    db.flush()
    db.add(WecomOutboundOutbox(
        inbound_event_id=inbound_event_id,
        reply_index=reply_index,
        userid=(None if conversation_type == "group" else userid),
        msg_type="text",
        content=None,
        recommendation_delivery_id=delivery.delivery_id,
        channel=source_channel,
        conversation_type=conversation_type,
        conversation_id=conversation_id,
        chat_id=chat_id,
        ordering_key=ordering_key,
        provider_req_id=provider_req_id,
        reply_command=("aibot_respond_msg" if aibot_reply else None),
        stream_id=outbox_stream_id,
        finish=outbox_finish,
        reply_expires_at=outbox_reply_expires_at,
        status="pending",
    ))
    return delivery


def persist_request_fact_only(
    db: Session,
    *,
    inbound_event_id: int,
    reply_index: int,
    userid: str,
    request_fact: Mapping[str, Any],
    source_inbound_msg_id: str | None = None,
    now: datetime | None = None,
) -> str | None:
    """Write request + attempt facts for a search that produced no delivery.

    §7.5: `off` mode, a legacy assignment and a zero-result search are all still
    real recommendation decisions and must appear in the request facts, but none
    of them writes a candidate into the reply — so there is no body to encrypt,
    no delivery to send and no impression to derive.  Without this path the
    zero-result rate is structurally 0 and legacy/off traffic share is invisible.
    """
    fact = dict(request_fact or {})
    if not fact:
        return None
    moment = ensure_utc(now) or utc_now()
    request_id = str(fact.get("request_id") or "") or str(uuid.uuid4())
    resolved_inbound_msg_id = str(
        source_inbound_msg_id or fact.get("source_inbound_msg_id") or inbound_event_id,
    )[:64]
    request_index = fact.get("request_index")
    request_index = reply_index if request_index is None else int(request_index)
    lock_and_validate_recommendation_targets(
        db, ctx={}, fact=fact, now=moment,
    )
    _persist_request_facts(
        db,
        request_id=request_id,
        source_inbound_msg_id=resolved_inbound_msg_id,
        request_index=request_index,
        userid=userid,
        snapshot_id=(fact.get("snapshot_id") or None),
        ctx={},
        fact=fact,
        items=[],
        now=moment,
    )
    return request_id


def mark_delivery_sent(
    db: Session, delivery_id: str, provider_msg_id: str | None = None,
) -> None:
    delivery = db.get(RecommendationDelivery, delivery_id)
    if not delivery:
        return
    sent_at = utc_now()
    delivery.status = "sent"
    delivery.wecom_msgid = provider_msg_id
    delivery.sent_at = to_naive_utc(sent_at)
    delivery.lease_owner = None
    delivery.lease_expires_at = None
    # §9.11：sent 之后正文最多再留 24 小时。
    apply_content_ttl(delivery, status="sent", terminal_at=sent_at)
    clear_session_patch(delivery)


def redact_delivery(db: Session, delivery_id: str) -> bool:
    """候选删除等场景下立即清正文（§10.1.1）。

    不改 ``status``：§9.6 的状态枚举里没有 ``redacted``，投递状态机与正文是否还在
    是两件事。
    """
    delivery = db.get(RecommendationDelivery, delivery_id)
    if not delivery:
        return False
    return purge_delivery_content(delivery)
