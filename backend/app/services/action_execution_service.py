"""Durable idempotency and lease/fencing helpers for v1 actions.

An action is identified by the immutable ``turn_id`` and stable
``action_name`` pair.  Claiming and finalizing deliberately do not call
``commit``: callers commit a successful claim before doing slow work, then
finalize the row in the same transaction as Session/business/outbox writes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping
from typing import Any, Literal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ActionExecution

ActionState = Literal[
    "acquired", "in_progress", "succeeded", "failed_retryable", "failed_terminal"
]
_FINAL_STATUSES = {"succeeded", "failed_retryable", "failed_terminal"}
SUPPORTED_ACTIONS = frozenset({"search_job", "show_more_job", "relax_job"})


class ActionExecutionConflict(ValueError):
    """The same idempotency key was reused with a different request."""


class ActionExecutionStateError(ValueError):
    """A durable row contains a status outside the v1 state machine."""


@dataclass(frozen=True)
class ActionClaim:
    """Outcome of a durable claim/read operation."""

    state: ActionState
    row: ActionExecution
    fencing_token: int
    result_digest: str | None = None

    @property
    def acquired(self) -> bool:
        return self.state == "acquired"

    @property
    def replay(self) -> bool:
        return self.state == "succeeded"

    @property
    def busy(self) -> bool:
        return self.state == "in_progress"


@dataclass(frozen=True)
class ActionResultReference:
    """Stable pointers needed to replay a committed action without rerunning it."""

    action_name: str
    turn_id: str
    request_id: str | None = None
    snapshot_id: str | None = None
    delivery_ids: tuple[str, ...] = ()
    outbox_ids: tuple[int | str, ...] = ()
    session_commit_id: str | None = None
    result_schema_version: str = "v1"
    result_ref_type: str = "recommendation"

    def as_dict(self) -> dict:
        return {
            "action_name": self.action_name,
            "turn_id": self.turn_id,
            "request_id": self.request_id,
            "snapshot_id": self.snapshot_id,
            "delivery_ids": list(self.delivery_ids),
            "outbox_ids": list(self.outbox_ids),
            "session_commit_id": self.session_commit_id,
            "result_schema_version": self.result_schema_version,
            "result_ref_type": self.result_ref_type,
        }


def build_result_reference(
    *,
    turn_id: str,
    action_name: str,
    request_id: str | None = None,
    snapshot_id: str | None = None,
    delivery_ids=None,
    outbox_ids=None,
    session_commit_id: str | None = None,
    result_schema_version: str = "v1",
    result_ref_type: str = "recommendation",
) -> ActionResultReference:
    """Normalize result pointers; result bodies never belong on ActionExecution."""
    if action_name not in SUPPORTED_ACTIONS:
        raise ValueError(f"unsupported_action:{action_name}")
    if not turn_id:
        raise ValueError("turn_id is required")
    deliveries = tuple(str(value) for value in (delivery_ids or ()) if value is not None)
    outboxes = tuple(value for value in (outbox_ids or ()) if value is not None)
    if result_ref_type not in {"recommendation", "terminal", "session"}:
        raise ValueError("invalid_result_ref_type")
    return ActionResultReference(
        action_name=action_name, turn_id=turn_id, request_id=request_id,
        snapshot_id=snapshot_id, delivery_ids=deliveries, outbox_ids=outboxes,
        session_commit_id=session_commit_id, result_schema_version=result_schema_version,
        result_ref_type=result_ref_type,
    )


def load_replay_reference(
    db: Session, turn_id: str, action_name: str, *, actor_userid: str | None = None,
) -> ActionResultReference:
    """Load and validate a succeeded reference; incomplete legacy rows fail closed."""
    row = read_action_execution(db, turn_id, action_name)
    if row is None or row.status != "succeeded":
        raise ActionExecutionStateError("action_not_replayable")
    if action_name not in SUPPORTED_ACTIONS:
        raise ActionExecutionStateError("unsupported_action")
    if row.turn_id != turn_id or not row.result_ref_type or not row.result_schema_version:
        raise ActionExecutionStateError("legacy_unreplayable")
    ref = build_result_reference(
        turn_id=row.turn_id, action_name=row.action_name, request_id=row.request_id,
        snapshot_id=row.snapshot_id, delivery_ids=row.delivery_ids, outbox_ids=row.outbox_ids,
        session_commit_id=row.session_commit_id, result_schema_version=row.result_schema_version,
        result_ref_type=row.result_ref_type,
    )
    if ref.result_ref_type == "recommendation" and not (ref.request_id or ref.snapshot_id or ref.delivery_ids or ref.outbox_ids):
        raise ActionExecutionStateError("result_reference_incomplete")
    return ref


def _naive_utc(value: datetime | None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def read_action_execution(
    db: Session,
    turn_id: str,
    action_name: str,
    *,
    for_update: bool = False,
) -> ActionExecution | None:
    """Read the durable action record, optionally taking a row lock."""
    query = db.query(ActionExecution).filter(
        ActionExecution.turn_id == turn_id,
        ActionExecution.action_name == action_name,
    )
    if for_update:
        query = query.with_for_update()
    return query.populate_existing().one_or_none()


def _state_for_row(row: ActionExecution, now: datetime) -> ActionState:
    if row.status == "succeeded":
        return "succeeded"
    if row.status == "failed_retryable":
        # Retryable failures reuse the same idempotency key and may be claimed
        # immediately; fencing advances when the new owner takes it.
        return "acquired"
    if row.status == "failed_terminal":
        return "failed_terminal"
    if row.status == "started":
        # A malformed started row with no deadline is not provably expired;
        # never let a recovery worker steal it.  The operator/reconciler can
        # repair such rows explicitly.
        if row.lease_until is None or row.lease_until > now:
            return "in_progress"
        return "acquired"
    # Never interpret schema drift/corruption as permission to execute.  A
    # caller may surface this for operator repair, but must not run the action.
    raise ActionExecutionStateError(f"unknown_action_status:{row.status!r}")


def claim_action_execution(
    db: Session,
    turn_id: str,
    action_name: str,
    owner: str,
    *,
    request_digest: str | None = None,
    lease_seconds: int = 180,
    now: datetime | None = None,
    action_version: str = "v1",
    parse_ref: str | None = None,
    parse_digest: str | None = None,
    parse_version: str | None = None,
    parse_expires_at: datetime | None = None,
) -> ActionClaim:
    """Create or claim an action execution record.

    A live ``started`` lease is never stolen.  An expired lease, or a
    ``failed_retryable`` record, is reclaimed by the caller and receives a new
    fencing token.  The unique key handles duplicate queue deliveries; the
    savepoint keeps an insert race from rolling back unrelated caller work.
    """
    if not turn_id or not action_name or not owner:
        raise ValueError("turn_id, action_name and owner are required")
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    now_value = _naive_utc(now)
    lease_until = now_value + timedelta(seconds=lease_seconds)

    row = read_action_execution(db, turn_id, action_name, for_update=True)
    if row is None:
        row = ActionExecution(
            turn_id=turn_id,
            action_name=action_name,
            status="started",
            request_digest=request_digest,
            lease_owner=owner,
            lease_until=lease_until,
            fencing_token=1,
            action_version=action_version,
            parse_ref=parse_ref,
            parse_digest=parse_digest,
            parse_version=parse_version,
            parse_expires_at=_naive_utc(parse_expires_at) if parse_expires_at else None,
        )
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError:
            # Another worker inserted the unique key.  Re-read it under a row
            # lock and apply the same state machine as an existing row.
            row = read_action_execution(db, turn_id, action_name, for_update=True)
            if row is None:
                raise
        else:
            return ActionClaim("acquired", row, 1, None)

    if request_digest is not None and row.request_digest not in (None, request_digest):
        raise ActionExecutionConflict("request_digest_mismatch")

    state = _state_for_row(row, now_value)
    if state != "acquired":
        return ActionClaim(
            state,
            row,
            int(row.fencing_token or 0),
            row.result_digest,
        )

    # Reclaiming either an expired started lease or a retryable failure fences
    # the previous worker.  The token is monotonic for the lifetime of a key.
    row.status = "started"
    row.lease_owner = owner
    row.lease_until = lease_until
    row.fencing_token = int(row.fencing_token or 0) + 1
    row.finished_at = None
    row.result_digest = None
    if request_digest is not None:
        row.request_digest = request_digest
    db.flush()
    return ActionClaim("acquired", row, int(row.fencing_token), None)


def finalize_action_execution(
    db: Session,
    turn_id: str,
    action_name: str,
    owner: str,
    fencing_token: int,
    *,
    status: str = "succeeded",
    result_digest: str | None = None,
    result_reference: ActionResultReference | Mapping[str, Any] | None = None,
    failure_code: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Finalize only when this worker still owns the live fenced lease.

    ``False`` means the lease was lost or the action was already finalized;
    callers must not commit business results when that happens.
    """
    if status not in _FINAL_STATUSES:
        raise ValueError(f"invalid action execution final status: {status}")
    now_value = _naive_utc(now)
    values = {
        "status": status,
        "result_digest": result_digest,
        "finished_at": now_value,
        "lease_owner": None,
        "lease_until": None,
        "failure_code": failure_code,
    }
    if result_reference is not None:
        ref = result_reference if isinstance(result_reference, ActionResultReference) else build_result_reference(**dict(result_reference))
        if ref.turn_id != turn_id or ref.action_name != action_name:
            raise ValueError("result_reference_binding_mismatch")
        values.update({
            "action_version": ref.result_schema_version,
            "result_ref_type": ref.result_ref_type,
            "request_id": ref.request_id,
            "snapshot_id": ref.snapshot_id,
            "delivery_ids": list(ref.delivery_ids),
            "outbox_ids": list(ref.outbox_ids),
            "session_commit_id": ref.session_commit_id,
            "result_schema_version": ref.result_schema_version,
        })
    updated = (
        db.query(ActionExecution)
        .filter(
            ActionExecution.turn_id == turn_id,
            ActionExecution.action_name == action_name,
            ActionExecution.status == "started",
            ActionExecution.lease_owner == owner,
            ActionExecution.fencing_token == int(fencing_token),
            ActionExecution.lease_until.isnot(None),
            ActionExecution.lease_until > now_value,
        )
        .update(
            values,
            synchronize_session=False,
        )
    )
    return updated == 1


__all__ = [
    "ActionClaim",
    "ActionExecutionConflict",
    "ActionExecutionStateError",
    "ActionResultReference",
    "SUPPORTED_ACTIONS",
    "build_result_reference",
    "claim_action_execution",
    "finalize_action_execution",
    "read_action_execution",
    "load_replay_reference",
]
