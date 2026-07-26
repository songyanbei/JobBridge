"""Strategy templates, immutable versions, release state and runtime control.

This module owns the推荐 v1 control plane:

* draft → published → archived lifecycle (§7.1);
* release mutations (mode/rollout/promote/rollback) with `lock_version` CAS and
  the immutable `recommendation_release_history` snapshot (§7.3/§7.4/§9.3);
* the dynamic kill switch distribution described in §7.5 — DB is the source of
  truth, Redis write-through + Pub/Sub is only the acceleration channel, and a
  process falls back to `off/legacy` whenever it cannot validate its local copy.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import event, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import redis_client
from app.core.time_utils import to_naive_utc, utc_now
from app.models import (
    RecommendationReleaseHistory,
    RecommendationRuntimeControl,
    RecommendationStrategyRelease,
    RecommendationStrategyVersion,
)
from app.schemas.recommendation import (
    RecommendationStrategyParameters,
    StrategyTemplate,
    TEMPLATE_DEFAULTS,
)

logger = logging.getLogger(__name__)

DIRECTIONS: tuple[str, ...] = ("search_job", "search_worker")

# §9.3 operation vocabulary.  `rollback` is the only operation allowed to carry
# a `target_revision`, and it is required there.
RELEASE_OPERATIONS: tuple[str, ...] = (
    "init", "publish_candidate", "mode_change", "rollout", "promote", "rollback",
)

# §7.5: a process may serve from its local控制值 for at most 5 seconds; after
# that a new recommendation must re-validate against the source of truth.
RUNTIME_CONTROL_MAX_AGE_SECONDS = 5.0
RUNTIME_CONTROL_POLL_SECONDS = 5.0

_VERSION_NO_MAX_RETRIES = 5


class ReleaseLockConflict(RuntimeError):
    """`lock_version` CAS 失败：另一个管理员已经改过这一行。"""


class ReleaseStateError(ValueError):
    """请求的发布状态迁移不满足方案前提条件。"""


def canonical_parameters(parameters: RecommendationStrategyParameters | dict[str, Any]) -> dict[str, Any]:
    if not isinstance(parameters, RecommendationStrategyParameters):
        parameters = RecommendationStrategyParameters.model_validate(parameters)
    return parameters.model_dump(mode="json")


def parameters_digest(parameters: RecommendationStrategyParameters | dict[str, Any]) -> str:
    payload = json.dumps(canonical_parameters(parameters), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def template_parameters(template_key: str) -> RecommendationStrategyParameters:
    if template_key not in TEMPLATE_DEFAULTS:
        raise ValueError(f"unknown recommendation template: {template_key}")
    return RecommendationStrategyParameters.from_template(template_key)


def validate_direction(direction: str) -> str:
    """Reject unknown推荐方向 before it reaches the ORM (P2-20)."""
    if direction not in DIRECTIONS:
        raise ReleaseStateError("unknown recommendation direction")
    return direction


# ---------------------------------------------------------------------------
# Immutable release history (§9.3)
# ---------------------------------------------------------------------------

def append_release_history(
    db: Session,
    *,
    direction: str,
    revision: int,
    operation: str,
    execution_mode: str,
    stable_version_id: int | None,
    candidate_version_id: int | None,
    rollout_percentage: int,
    change_reason: str,
    created_by: str,
    target_revision: int | None = None,
) -> RecommendationReleaseHistory:
    """Insert one immutable snapshot, enforcing the §9.3 constraints in code.

    The matching database CHECK lives in the migration; this guard keeps the
    invariant true for SQLite/unit environments and turns a silent data defect
    into an immediate error at the only insertion point.
    """
    if operation not in RELEASE_OPERATIONS:
        raise ReleaseStateError(f"unknown release history operation: {operation}")
    if operation == "rollback" and target_revision is None:
        raise ReleaseStateError("rollback release history requires target_revision")
    if operation != "rollback" and target_revision is not None:
        raise ReleaseStateError("only rollback release history may carry target_revision")
    row = RecommendationReleaseHistory(
        direction=direction,
        revision=revision,
        operation=operation,
        execution_mode=execution_mode,
        stable_version_id=stable_version_id,
        candidate_version_id=candidate_version_id,
        rollout_percentage=rollout_percentage,
        target_revision=target_revision,
        change_reason=change_reason,
        created_by=created_by,
    )
    db.add(row)
    return row


@event.listens_for(RecommendationReleaseHistory, "before_update", propagate=True)
def _release_history_is_insert_only(mapper, connection, target):  # pragma: no cover - guard
    raise ReleaseStateError("recommendation_release_history rows are insert-only")


@event.listens_for(RecommendationReleaseHistory, "before_delete", propagate=True)
def _release_history_is_not_deletable(mapper, connection, target):  # pragma: no cover - guard
    raise ReleaseStateError("recommendation_release_history rows are insert-only")


def ensure_initial_release(db: Session, *, updated_by: str = "system") -> bool:
    """Create the legacy baseline rows if missing.  Returns True when it wrote.

    Callers on read-only endpoints must check the return value before issuing a
    ``commit`` so a viewer's GET does not carry a write (P2-23).
    """
    written = False
    for direction in DIRECTIONS:
        if not db.get(RecommendationStrategyRelease, direction):
            db.add(RecommendationStrategyRelease(
                direction=direction,
                execution_mode="off",
                stable_version_id=None,
                candidate_version_id=None,
                rollout_percentage=0,
                revision=1,
                lock_version=1,
                updated_by=updated_by,
            ))
            append_release_history(
                db,
                direction=direction,
                revision=1,
                operation="init",
                execution_mode="off",
                stable_version_id=None,
                candidate_version_id=None,
                rollout_percentage=0,
                change_reason="initial legacy baseline",
                created_by=updated_by,
            )
            written = True
    if not db.get(RecommendationRuntimeControl, "global"):
        db.add(RecommendationRuntimeControl(
            scope="global",
            kill_switch=False,
            revision=1,
            lock_version=1,
            change_reason="initial",
            updated_by=updated_by,
        ))
        written = True
    return written


# ---------------------------------------------------------------------------
# Strategy versions
# ---------------------------------------------------------------------------

def create_draft(
    db: Session,
    *,
    direction: str,
    template_key: str,
    parameters: RecommendationStrategyParameters | dict[str, Any] | None = None,
    created_by: str,
    change_reason: str,
    base_version_id: int | None = None,
) -> RecommendationStrategyVersion:
    validate_direction(direction)
    params = (
        template_parameters(template_key)
        if parameters is None
        else RecommendationStrategyParameters.model_validate(parameters)
    )
    digest = parameters_digest(params)
    canonical = canonical_parameters(params)
    # `max(version_no)+1` races two concurrent admins onto the same number; the
    # unique (direction, version_no) index is the real arbiter, so retry against
    # a savepoint instead of pretending the read was atomic (P2-20).
    last_error: Exception | None = None
    for _ in range(_VERSION_NO_MAX_RETRIES):
        version_no = (
            db.query(func.max(RecommendationStrategyVersion.version_no))
            .filter(RecommendationStrategyVersion.direction == direction)
            .scalar()
            or 0
        ) + 1
        row = RecommendationStrategyVersion(
            direction=direction,
            version_no=version_no,
            template_key=template_key,
            status="draft",
            parameters=canonical,
            parameters_digest=digest,
            algorithm_version="recommendation-v1",
            base_version_id=base_version_id,
            lock_version=1,
            change_reason=change_reason,
            created_by=created_by,
        )
        savepoint = db.begin_nested()
        try:
            db.add(row)
            db.flush()
        except IntegrityError as exc:
            last_error = exc
            savepoint.rollback()
            try:
                db.expunge(row)
            except Exception:
                # The savepoint rollback normally detaches it already.
                pass
            continue
        savepoint.commit()
        return row
    raise ReleaseStateError("could not allocate a unique strategy version_no") from last_error


def update_draft(
    db: Session,
    *,
    version_id: int,
    lock_version: int,
    template_key: str,
    parameters: RecommendationStrategyParameters | dict[str, Any],
    change_reason: str,
) -> RecommendationStrategyVersion:
    row = db.get(RecommendationStrategyVersion, version_id)
    if not row or row.status != "draft":
        raise ValueError("only draft strategy versions can be edited")
    params = RecommendationStrategyParameters.model_validate(parameters)
    updated = (
        db.query(RecommendationStrategyVersion)
        .filter(
            RecommendationStrategyVersion.id == version_id,
            RecommendationStrategyVersion.status == "draft",
            RecommendationStrategyVersion.lock_version == lock_version,
        )
        .update(
            {
                "template_key": template_key,
                "parameters": canonical_parameters(params),
                "parameters_digest": parameters_digest(params),
                "last_simulated_digest": None,
                "last_simulated_at": None,
                "change_reason": change_reason,
                "lock_version": RecommendationStrategyVersion.lock_version + 1,
            },
            synchronize_session=False,
        )
    )
    if not updated:
        raise RuntimeError("strategy_version_lock_conflict")
    db.expire(row)
    return row


def mark_simulated(db: Session, version_id: int, digest: str | None = None) -> RecommendationStrategyVersion:
    row = db.get(RecommendationStrategyVersion, version_id)
    if not row or row.status != "draft":
        raise ValueError("draft strategy version not found")
    row.last_simulated_digest = digest or row.parameters_digest
    row.last_simulated_at = to_naive_utc(utc_now())
    db.flush()
    return row


def publish_candidate(
    db: Session,
    *,
    version_id: int,
    operator: str,
    change_reason: str | None = None,
    lock_version: int | None = None,
) -> RecommendationStrategyVersion:
    row = db.get(RecommendationStrategyVersion, version_id)
    if not row or row.status != "draft":
        raise ValueError("draft strategy version not found")
    if lock_version is not None and row.lock_version != lock_version:
        raise RuntimeError("strategy_version_lock_conflict")
    if row.last_simulated_digest != row.parameters_digest:
        raise ValueError("strategy_must_be_simulated_after_last_change")
    published_at = to_naive_utc(utc_now())
    updated = (
        db.query(RecommendationStrategyVersion)
        .filter(
            RecommendationStrategyVersion.id == version_id,
            RecommendationStrategyVersion.status == "draft",
            RecommendationStrategyVersion.lock_version == row.lock_version,
        )
        .update(
            {
                "status": "published",
                "published_by": operator,
                "published_at": published_at,
                "change_reason": change_reason or row.change_reason,
                "lock_version": RecommendationStrategyVersion.lock_version + 1,
            },
            synchronize_session=False,
        )
    )
    if not updated:
        raise RuntimeError("strategy_version_lock_conflict")
    db.expire(row)
    return row


def strategy_version_payload(row: RecommendationStrategyVersion) -> dict[str, Any]:
    """The immutable subset the serving path needs (§11.8 cache contract)."""
    return {
        "id": int(row.id),
        "direction": row.direction,
        "version_no": int(row.version_no),
        "template_key": row.template_key,
        "status": row.status,
        "parameters": row.parameters,
        "parameters_digest": row.parameters_digest,
        "algorithm_version": row.algorithm_version or "recommendation-v1",
    }


def load_published_version(db: Session, version_id: int | None) -> RecommendationStrategyVersion | None:
    """Read an immutable version, preferring the 10-minute Redis cache (§11.8).

    Only `published`/`archived` rows are cacheable — drafts stay mutable, and a
    release pointer is never cached at all.  A cache hit returns a **detached**
    ORM instance so callers keep the same attribute contract.
    """
    if not version_id:
        return None
    cached = redis_client.get_cached_strategy_version(version_id)
    if cached and cached.get("status") in ("published", "archived"):
        try:
            return RecommendationStrategyVersion(**cached)
        except Exception:  # pragma: no cover - corrupted cache must not break serving
            logger.warning("discarding malformed strategy version cache", exc_info=True)
    row = db.get(RecommendationStrategyVersion, version_id)
    if row is not None and row.status in ("published", "archived"):
        redis_client.set_cached_strategy_version(version_id, strategy_version_payload(row))
    return row


# ---------------------------------------------------------------------------
# Release mutations (§7.3 / §7.4 / §9.2)
# ---------------------------------------------------------------------------

def release_snapshot(row: RecommendationStrategyRelease | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "direction": row.direction,
        "execution_mode": row.execution_mode,
        "stable_version_id": row.stable_version_id,
        "candidate_version_id": row.candidate_version_id,
        "rollout_percentage": row.rollout_percentage,
        "revision": row.revision,
        "lock_version": row.lock_version,
        "updated_by": row.updated_by,
        "updated_at": row.updated_at,
    }


def _cas_release(
    db: Session,
    row: RecommendationStrategyRelease,
    *,
    expected_lock_version: int,
    values: dict[str, Any],
) -> RecommendationStrategyRelease:
    """Compare-and-swap on `lock_version`; never read-then-write (P2-18)."""
    updated = (
        db.query(RecommendationStrategyRelease)
        .filter(
            RecommendationStrategyRelease.direction == row.direction,
            RecommendationStrategyRelease.lock_version == expected_lock_version,
        )
        .update(
            {
                **values,
                "revision": RecommendationStrategyRelease.revision + 1,
                "lock_version": RecommendationStrategyRelease.lock_version + 1,
            },
            synchronize_session=False,
        )
    )
    if not updated:
        raise ReleaseLockConflict("release lock_version conflict")
    db.expire(row)
    return row


def _history_operation(before: dict[str, Any], after_mode: str, after_rollout: int) -> str:
    """§9.3 distinguishes a mode switch from a pure ratio change (P2-17)."""
    if before.get("execution_mode") != after_mode:
        return "mode_change"
    if int(before.get("rollout_percentage") or 0) != int(after_rollout):
        return "rollout"
    return "mode_change"


def update_release(
    db: Session,
    *,
    direction: str,
    execution_mode: str,
    candidate_version_id: int | None,
    rollout_percentage: int,
    lock_version: int,
    change_reason: str,
    operator: str,
) -> tuple[RecommendationStrategyRelease, dict[str, Any]]:
    validate_direction(direction)
    row = db.get(RecommendationStrategyRelease, direction)
    if not row:
        ensure_initial_release(db, updated_by=operator)
        db.flush()
        row = db.get(RecommendationStrategyRelease, direction)
    if execution_mode != "off" and candidate_version_id is None:
        raise ReleaseStateError("shadow/on 必须指定候选版本")
    if candidate_version_id is not None:
        candidate = db.get(RecommendationStrategyVersion, candidate_version_id)
        if not candidate or candidate.status != "published" or candidate.direction != direction:
            raise ReleaseStateError("candidate version must be published and match direction")
    before = release_snapshot(row)
    operation = _history_operation(before or {}, execution_mode, rollout_percentage)
    _cas_release(
        db, row, expected_lock_version=lock_version,
        values={
            "execution_mode": execution_mode,
            "candidate_version_id": candidate_version_id,
            "rollout_percentage": rollout_percentage,
            "updated_by": operator,
        },
    )
    append_release_history(
        db,
        direction=direction,
        revision=row.revision,
        operation=operation,
        execution_mode=row.execution_mode,
        stable_version_id=row.stable_version_id,
        candidate_version_id=row.candidate_version_id,
        rollout_percentage=row.rollout_percentage,
        change_reason=change_reason,
        created_by=operator,
    )
    return row, before or {}


def record_candidate_publication(
    db: Session,
    *,
    direction: str,
    version_id: int,
    lock_version: int,
    change_reason: str,
    operator: str,
) -> tuple[RecommendationStrategyRelease | None, dict[str, Any]]:
    """§9.3 requires a history snapshot for `publish_candidate` too (P2-17).

    Publishing a version does not move any release pointer, so the snapshot is
    the *current* state at a new revision; that keeps `revision` a complete,
    gap-free audit trail of the control plane.
    """
    validate_direction(direction)
    row = db.get(RecommendationStrategyRelease, direction)
    if not row:
        ensure_initial_release(db, updated_by=operator)
        db.flush()
        row = db.get(RecommendationStrategyRelease, direction)
    before = release_snapshot(row) or {}
    _cas_release(db, row, expected_lock_version=lock_version, values={"updated_by": operator})
    append_release_history(
        db,
        direction=direction,
        revision=row.revision,
        operation="publish_candidate",
        execution_mode=row.execution_mode,
        stable_version_id=row.stable_version_id,
        candidate_version_id=row.candidate_version_id,
        rollout_percentage=row.rollout_percentage,
        change_reason=f"{change_reason} (version_id={version_id})",
        created_by=operator,
    )
    return row, before


def promote_release(
    db: Session,
    *,
    direction: str,
    lock_version: int,
    change_reason: str,
    operator: str,
) -> tuple[RecommendationStrategyRelease, dict[str, Any], int | None]:
    """§7.3: candidate → stable, 旧 stable 归档, 清空候选指针.

    Returns ``(release, before_snapshot, archived_version_id)``.
    """
    validate_direction(direction)
    row = db.get(RecommendationStrategyRelease, direction)
    if not row or not row.candidate_version_id:
        raise ReleaseStateError("no candidate version to promote")
    # §7.3 only allows a promote after the候选 has been at on/100%.  Without the
    # check a 5% candidate could be promoted to 100% of traffic in one click.
    if row.execution_mode != "on" or int(row.rollout_percentage or 0) != 100:
        raise ReleaseStateError("promote requires execution_mode=on at rollout_percentage=100")
    candidate = db.get(RecommendationStrategyVersion, row.candidate_version_id)
    if not candidate or candidate.status != "published" or candidate.direction != direction:
        raise ReleaseStateError("invalid candidate version")
    before = release_snapshot(row) or {}
    previous_stable_id = row.stable_version_id
    _cas_release(
        db, row, expected_lock_version=lock_version,
        values={
            "stable_version_id": row.candidate_version_id,
            "candidate_version_id": None,
            "execution_mode": "on",
            "rollout_percentage": 0,
            "updated_by": operator,
        },
    )
    archived_version_id = archive_version(db, previous_stable_id)
    append_release_history(
        db,
        direction=direction,
        revision=row.revision,
        operation="promote",
        execution_mode=row.execution_mode,
        stable_version_id=row.stable_version_id,
        candidate_version_id=None,
        rollout_percentage=row.rollout_percentage,
        change_reason=change_reason,
        created_by=operator,
    )
    return row, before, archived_version_id


def archive_version(db: Session, version_id: int | None) -> int | None:
    """Move a published version to `archived` (§7.1); idempotent and safe on None."""
    if not version_id:
        return None
    updated = (
        db.query(RecommendationStrategyVersion)
        .filter(
            RecommendationStrategyVersion.id == version_id,
            RecommendationStrategyVersion.status == "published",
        )
        .update({"status": "archived"}, synchronize_session=False)
    )
    if not updated:
        return None
    stale = db.get(RecommendationStrategyVersion, version_id)
    if stale is not None:
        db.expire(stale)
    return int(version_id)


def rollback_release(
    db: Session,
    *,
    direction: str,
    target_revision: int,
    lock_version: int,
    change_reason: str,
    operator: str,
) -> tuple[RecommendationStrategyRelease, dict[str, Any]]:
    """§7.4: copy an immutable history snapshot forward as a *new* revision."""
    validate_direction(direction)
    row = db.get(RecommendationStrategyRelease, direction)
    target = (
        db.query(RecommendationReleaseHistory)
        .filter_by(direction=direction, revision=target_revision)
        .first()
    )
    if not row or not target:
        raise ReleaseStateError("rollback target revision not found")
    before = release_snapshot(row) or {}
    # A rollback target may legitimately point at a version that was archived by
    # a later promote; §7.1 allows re-publishing an archived version.
    for version_id in (target.stable_version_id, target.candidate_version_id):
        _restore_version_for_rollback(db, version_id)
    _cas_release(
        db, row, expected_lock_version=lock_version,
        values={
            "execution_mode": target.execution_mode,
            "stable_version_id": target.stable_version_id,
            "candidate_version_id": target.candidate_version_id,
            "rollout_percentage": target.rollout_percentage,
            "updated_by": operator,
        },
    )
    append_release_history(
        db,
        direction=direction,
        revision=row.revision,
        operation="rollback",
        execution_mode=row.execution_mode,
        stable_version_id=row.stable_version_id,
        candidate_version_id=row.candidate_version_id,
        rollout_percentage=row.rollout_percentage,
        target_revision=target_revision,
        change_reason=change_reason,
        created_by=operator,
    )
    return row, before


def _restore_version_for_rollback(db: Session, version_id: int | None) -> None:
    if not version_id:
        return
    db.query(RecommendationStrategyVersion).filter(
        RecommendationStrategyVersion.id == version_id,
        RecommendationStrategyVersion.status == "archived",
    ).update({"status": "published"}, synchronize_session=False)
    stale = db.get(RecommendationStrategyVersion, version_id)
    if stale is not None:
        db.expire(stale)


# ---------------------------------------------------------------------------
# Runtime control / kill switch (§7.5 / §9.3.1 / §11.8)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RuntimeControlState:
    kill_switch: bool
    revision: int
    source: str
    checked_at: float
    verified: bool = True

    def age_seconds(self, now: float | None = None) -> float:
        return (time.monotonic() if now is None else now) - self.checked_at

    def is_fresh(self, now: float | None = None, max_age: float = RUNTIME_CONTROL_MAX_AGE_SECONDS) -> bool:
        return self.verified and self.age_seconds(now) <= max_age


_control_lock = threading.RLock()
_control_state: RuntimeControlState | None = None
_env_override: bool | None = None


def env_kill_switch_override() -> bool:
    """`RECOMMENDATION_STRATEGY_KILL_SWITCH=true` 是进程启动时的更强 override.

    §7.5: the environment variable can only ever *add* a kill; a `false` value
    must never be able to override a `true` coming from the database.  The value
    is therefore latched on first read so a mid-process `.env` edit cannot
    silently lift an emergency stop without the required rolling restart.
    """
    global _env_override
    if _env_override is None:
        try:
            from app.config import settings

            _env_override = bool(getattr(settings, "recommendation_strategy_kill_switch", False))
        except Exception:  # pragma: no cover - config import must never break serving
            _env_override = False
    return _env_override


def reset_runtime_control_cache() -> None:
    """Test hook: drop the process-local控制值 and the latched env override."""
    global _control_state, _env_override
    with _control_lock:
        _control_state = None
        _env_override = None


def runtime_control_state() -> RuntimeControlState | None:
    with _control_lock:
        return _control_state


def _parse_control_payload(payload: Any) -> tuple[bool, int] | None:
    if not isinstance(payload, dict):
        return None
    if "revision" not in payload or "kill_switch" not in payload:
        return None
    try:
        revision = int(payload["revision"])
    except (TypeError, ValueError):
        return None
    raw = payload["kill_switch"]
    if isinstance(raw, str):
        kill = raw.strip().lower() in ("1", "true", "yes", "on")
    else:
        kill = bool(raw)
    return kill, revision


def apply_runtime_control_update(payload: Any, *, source: str = "pubsub") -> RuntimeControlState | None:
    """Accept an update only when its revision is ≥ the local one (§7.5).

    Pub/Sub can deliver out of order and the 5-second DB poll races with it, so
    monotonicity — not arrival order — decides which value wins.
    """
    global _control_state
    parsed = _parse_control_payload(payload)
    if parsed is None:
        return None
    kill, revision = parsed
    with _control_lock:
        current = _control_state
        if current is not None and current.verified and revision < current.revision:
            return current
        _control_state = RuntimeControlState(
            kill_switch=kill,
            revision=revision,
            source=source,
            checked_at=time.monotonic(),
        )
        return _control_state


def refresh_runtime_control_from_db(db: Session) -> RuntimeControlState | None:
    row = db.get(RecommendationRuntimeControl, "global")
    if row is None:
        # Not initialised yet: nothing has ever been killed, but keep revision 0
        # so the first real toggle (revision 1) still wins.
        return apply_runtime_control_update({"kill_switch": False, "revision": 0}, source="db")
    return apply_runtime_control_update(
        {"kill_switch": bool(row.kill_switch), "revision": int(row.revision)}, source="db",
    )


def refresh_runtime_control_from_redis() -> RuntimeControlState | None:
    payload = redis_client.read_runtime_control()
    if not payload:
        return None
    return apply_runtime_control_update(payload, source="redis")


def _fail_safe_state(previous: RuntimeControlState | None) -> RuntimeControlState:
    """DB and Redis both unusable → force off/legacy (§7.5 read failure policy)."""
    return RuntimeControlState(
        kill_switch=True,
        revision=previous.revision if previous else 0,
        source="fail_safe",
        checked_at=time.monotonic(),
        verified=False,
    )


def resolve_runtime_control(db: Session | None = None) -> RuntimeControlState:
    """Return the控制值 a new recommendation must serve under.

    Order: latched env override → fresh process-local value → DB → Redis →
    fail-safe kill.  The fail-safe state is deliberately **not** stored, so a
    transient outage cannot pin the process to a bogus revision.
    """
    if env_kill_switch_override():
        with _control_lock:
            previous = _control_state
        return RuntimeControlState(
            kill_switch=True,
            revision=previous.revision if previous else 0,
            source="env",
            checked_at=time.monotonic(),
        )
    with _control_lock:
        state = _control_state
    if state is not None and state.is_fresh():
        return state
    if db is not None:
        try:
            refreshed = refresh_runtime_control_from_db(db)
            if refreshed is not None:
                return refreshed
        except Exception:
            logger.warning("runtime control DB refresh failed", exc_info=True)
    refreshed = refresh_runtime_control_from_redis()
    if refreshed is not None:
        return refreshed
    return _fail_safe_state(state)


def runtime_kill_switch(db: Session | None = None) -> bool:
    """True when新策略 must not run — the single read entry for the serving path."""
    return resolve_runtime_control(db).kill_switch


def broadcast_runtime_control(*, kill_switch: bool, revision: int) -> bool:
    """write-through Redis + Pub/Sub after the DB transaction committed (§7.5)."""
    payload = {
        "kill_switch": bool(kill_switch),
        "revision": int(revision),
        "updated_at": utc_now().isoformat(),
    }
    apply_runtime_control_update(payload, source="local_write")
    return redis_client.publish_runtime_control(payload)


def set_kill_switch(
    db: Session,
    *,
    enabled: bool,
    lock_version: int,
    change_reason: str,
    operator: str,
) -> tuple[RecommendationRuntimeControl, dict[str, Any]]:
    """CAS the runtime control row; caller commits and then broadcasts."""
    row = db.get(RecommendationRuntimeControl, "global")
    if not row:
        row = RecommendationRuntimeControl(
            scope="global", kill_switch=False, revision=1, lock_version=1,
            updated_by="system", change_reason="initial",
        )
        db.add(row)
        db.flush()
    before = {
        "kill_switch": bool(row.kill_switch),
        "revision": int(row.revision),
        "lock_version": int(row.lock_version),
        "change_reason": row.change_reason,
        "updated_by": row.updated_by,
    }
    updated = (
        db.query(RecommendationRuntimeControl)
        .filter(
            RecommendationRuntimeControl.scope == "global",
            RecommendationRuntimeControl.lock_version == lock_version,
        )
        .update(
            {
                "kill_switch": bool(enabled),
                "change_reason": change_reason,
                "updated_by": operator,
                "revision": RecommendationRuntimeControl.revision + 1,
                "lock_version": RecommendationRuntimeControl.lock_version + 1,
            },
            synchronize_session=False,
        )
    )
    if not updated:
        raise ReleaseLockConflict("kill switch lock_version conflict")
    db.expire(row)
    return row, before


_watcher_thread: threading.Thread | None = None
_watcher_stop: threading.Event | None = None


def start_runtime_control_watcher(
    session_factory: Callable[[], Session] | None = None,
    *,
    poll_seconds: float = RUNTIME_CONTROL_POLL_SECONDS,
) -> threading.Thread | None:
    """Subscribe to Pub/Sub **and** poll the DB every 5 seconds (§7.5).

    Pub/Sub alone loses messages on reconnect, so the poll is the convergence
    guarantee, not an optimisation.  App and every Worker process must call this
    once at startup.
    """
    global _watcher_thread, _watcher_stop
    with _control_lock:
        if _watcher_thread is not None and _watcher_thread.is_alive():
            return _watcher_thread
        if session_factory is None:
            from app.db import SessionLocal

            session_factory = SessionLocal
        stop = threading.Event()
        thread = threading.Thread(
            target=_runtime_control_loop,
            args=(session_factory, stop, poll_seconds),
            name="recommendation-runtime-control",
            daemon=True,
        )
        _watcher_thread, _watcher_stop = thread, stop
    thread.start()
    return thread


def stop_runtime_control_watcher(timeout: float = 2.0) -> None:
    global _watcher_thread, _watcher_stop
    with _control_lock:
        thread, stop = _watcher_thread, _watcher_stop
        _watcher_thread, _watcher_stop = None, None
    if stop is not None:
        stop.set()
    if thread is not None:
        thread.join(timeout=timeout)


def _poll_runtime_control_once(session_factory: Callable[[], Session]) -> None:
    db = session_factory()
    try:
        refresh_runtime_control_from_db(db)
    finally:
        db.close()


def _runtime_control_loop(
    session_factory: Callable[[], Session],
    stop: threading.Event,
    poll_seconds: float,
) -> None:  # pragma: no cover - thread body exercised by integration runs
    pubsub = redis_client.runtime_control_pubsub()
    last_poll = 0.0
    while not stop.is_set():
        now = time.monotonic()
        if now - last_poll >= poll_seconds:
            last_poll = now
            try:
                _poll_runtime_control_once(session_factory)
            except Exception:
                logger.warning("runtime control poll failed", exc_info=True)
        if pubsub is None:
            stop.wait(min(poll_seconds, 1.0))
            pubsub = redis_client.runtime_control_pubsub()
            continue
        try:
            message = pubsub.get_message(timeout=1.0)
        except Exception:
            logger.warning("runtime control subscription lost", exc_info=True)
            try:
                pubsub.close()
            except Exception:
                pass
            pubsub = None
            continue
        if not message or message.get("type") != "message":
            continue
        try:
            apply_runtime_control_update(json.loads(message.get("data") or "{}"))
        except Exception:
            logger.warning("malformed runtime control message", exc_info=True)
    if pubsub is not None:
        try:
            pubsub.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Assignment (§7.2 / §7.5)
# ---------------------------------------------------------------------------

def _stable_assignment(release: RecommendationStrategyRelease) -> tuple[str, int | None]:
    """`stable_version_id=NULL` 明确表示 legacy，不是配置错误（§7.2）。"""
    if release.stable_version_id:
        return "stable", release.stable_version_id
    return "legacy", None


def select_assignment(
    *,
    release: RecommendationStrategyRelease | None,
    userid: str,
    direction: str,
    kill_switch: bool = False,
) -> tuple[str, int | None]:
    """Decide what the **user-facing** reply is ranked with.

    §7.5 mode table: shadow always serves the稳定对照 (which is legacy when
    `stable_version_id` is NULL) — the candidate double-compute exists purely for
    offline diff evaluation and must not split one mode into two experiences.
    """
    if kill_switch or not release or release.execution_mode == "off":
        return "legacy", None
    if release.execution_mode == "shadow":
        return _stable_assignment(release)
    if release.candidate_version_id is None:
        return _stable_assignment(release)
    from app.services.recommendation_scoring_service import bucket_hit

    if bucket_hit(release.rollout_percentage, userid, direction, release.candidate_version_id):
        return "candidate", release.candidate_version_id
    return _stable_assignment(release)


def shadow_candidate_version_id(
    *,
    release: RecommendationStrategyRelease | None,
    userid: str,
    direction: str,
    kill_switch: bool = False,
) -> int | None:
    """Version the shadow executor must double-compute for this request.

    Returns None when no shadow work is due: kill switch on, mode ≠ shadow, no
    candidate bound, or the user missed the shadow bucket (§7.5 "未命中 shadow
    桶只执行 legacy，降低 LLM 和计算成本").
    """
    if kill_switch or not release or release.execution_mode != "shadow":
        return None
    if release.candidate_version_id is None:
        return None
    from app.services.recommendation_scoring_service import bucket_hit

    if not bucket_hit(release.rollout_percentage, userid, direction, release.candidate_version_id):
        return None
    return int(release.candidate_version_id)


def snapshot_is_invalidated_by_kill_switch(
    *,
    algorithm_version: str | None,
    kill_switch: bool,
) -> bool:
    """§7.5: once kill=true, a stored v1 snapshot must not be paged further.

    `show_more` calls this before consuming a snapshot; True means "drop the
    snapshot, rebuild with legacy under the original criteria and keep excluding
    the already shown IDs".
    """
    if not kill_switch:
        return False
    return bool(algorithm_version) and algorithm_version != "legacy"


__all__ = [
    "DIRECTIONS",
    "RELEASE_OPERATIONS",
    "ReleaseLockConflict",
    "ReleaseStateError",
    "RuntimeControlState",
    "append_release_history",
    "apply_runtime_control_update",
    "archive_version",
    "broadcast_runtime_control",
    "canonical_parameters",
    "create_draft",
    "ensure_initial_release",
    "env_kill_switch_override",
    "load_published_version",
    "mark_simulated",
    "parameters_digest",
    "promote_release",
    "publish_candidate",
    "record_candidate_publication",
    "refresh_runtime_control_from_db",
    "refresh_runtime_control_from_redis",
    "release_snapshot",
    "reset_runtime_control_cache",
    "resolve_runtime_control",
    "rollback_release",
    "runtime_control_state",
    "runtime_kill_switch",
    "select_assignment",
    "set_kill_switch",
    "shadow_candidate_version_id",
    "snapshot_is_invalidated_by_kill_switch",
    "start_runtime_control_watcher",
    "stop_runtime_control_watcher",
    "strategy_version_payload",
    "template_parameters",
    "update_draft",
    "update_release",
    "validate_direction",
]
