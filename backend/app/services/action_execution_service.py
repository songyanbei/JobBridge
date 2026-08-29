"""Durable idempotency and lease/fencing helpers for v1 actions.

An action is identified by the immutable ``turn_id`` and stable
``action_name`` pair.  Claiming and finalizing deliberately do not call
``commit``: callers commit a successful claim before doing slow work, then
finalize the row in the same transaction as Session/business/outbox writes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ActionExecution

ActionState = Literal[
    "acquired", "in_progress", "succeeded", "failed_retryable", "failed_terminal"
]
_FINAL_STATUSES = {"succeeded", "failed_retryable", "failed_terminal"}


class ActionExecutionConflict(ValueError):
    """The same idempotency key was reused with a different request."""


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
        if row.lease_until is None or row.lease_until >= now:
            return "in_progress"
        return "acquired"
    return "acquired"


def claim_action_execution(
    db: Session,
    turn_id: str,
    action_name: str,
    owner: str,
    *,
    request_digest: str | None = None,
    lease_seconds: int = 180,
    now: datetime | None = None,
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
    now: datetime | None = None,
) -> bool:
    """Finalize only when this worker still owns the live fenced lease.

    ``False`` means the lease was lost or the action was already finalized;
    callers must not commit business results when that happens.
    """
    if status not in _FINAL_STATUSES:
        raise ValueError(f"invalid action execution final status: {status}")
    now_value = _naive_utc(now)
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
            {
                "status": status,
                "result_digest": result_digest,
                "finished_at": now_value,
                "lease_owner": None,
                "lease_until": None,
            },
            synchronize_session=False,
        )
    )
    return updated == 1


__all__ = [
    "ActionClaim",
    "ActionExecutionConflict",
    "claim_action_execution",
    "finalize_action_execution",
    "read_action_execution",
]
