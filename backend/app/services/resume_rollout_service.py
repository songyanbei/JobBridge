"""Durable, request-scoped rollout assignment for resume replacement."""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.models import ResumeReplacementRolloutAssignment, SystemConfig
from app.services.resume_replacement_rollout_service import ROLLOUT_CONFIG_KEY, validate_allowlist


def assign_operation(
    db: Session, *, operation_id: str, source_msg_id: str, owner_userid: str,
) -> ResumeReplacementRolloutAssignment:
    existing = db.query(ResumeReplacementRolloutAssignment).filter(
        (ResumeReplacementRolloutAssignment.operation_id == operation_id)
        | (ResumeReplacementRolloutAssignment.source_msg_id == source_msg_id)
    ).first()
    if existing is not None:
        if existing.owner_userid != owner_userid:
            raise ValueError("rollout_assignment_idempotency_mismatch")
        return existing
    # The hidden config row is the smallest existing serialization point.  It
    # makes concurrent delivery of the same message converge before either
    # transaction attempts the unique assignment insert.
    config = db.query(SystemConfig).filter(
        SystemConfig.config_key == ROLLOUT_CONFIG_KEY
    ).with_for_update().one()
    existing = db.query(ResumeReplacementRolloutAssignment).filter(
        (ResumeReplacementRolloutAssignment.operation_id == operation_id)
        | (ResumeReplacementRolloutAssignment.source_msg_id == source_msg_id)
    ).first()
    if existing is not None:
        if existing.owner_userid != owner_userid:
            raise ValueError("rollout_assignment_idempotency_mismatch")
        return existing
    allowlist = validate_allowlist(json.loads(config.config_value))
    row = ResumeReplacementRolloutAssignment(
        operation_id=operation_id,
        source_msg_id=source_msg_id,
        owner_userid=owner_userid,
        cohort="enabled" if owner_userid in allowlist.userids else "control",
        allowlist_revision=allowlist.revision,
    )
    db.add(row)
    db.flush()
    return row
