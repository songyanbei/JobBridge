"""Strategy templates, immutable versions and release state."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

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


def ensure_initial_release(db: Session, *, updated_by: str = "system") -> None:
    for direction in ("search_job", "search_worker"):
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
            db.add(RecommendationReleaseHistory(
                direction=direction,
                revision=1,
                operation="init",
                execution_mode="off",
                stable_version_id=None,
                candidate_version_id=None,
                rollout_percentage=0,
                change_reason="initial legacy baseline",
                created_by=updated_by,
            ))
    if not db.get(RecommendationRuntimeControl, "global"):
        db.add(RecommendationRuntimeControl(
            scope="global",
            kill_switch=False,
            revision=1,
            lock_version=1,
            change_reason="initial",
            updated_by=updated_by,
        ))


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
    params = (
        template_parameters(template_key)
        if parameters is None
        else RecommendationStrategyParameters.model_validate(parameters)
    )
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
        parameters=canonical_parameters(params),
        parameters_digest=parameters_digest(params),
        algorithm_version="recommendation-v1",
        base_version_id=base_version_id,
        lock_version=1,
        change_reason=change_reason,
        created_by=created_by,
    )
    db.add(row)
    db.flush()
    return row


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
    if row.lock_version != lock_version:
        raise RuntimeError("strategy_version_lock_conflict")
    params = RecommendationStrategyParameters.model_validate(parameters)
    row.template_key = template_key
    row.parameters = canonical_parameters(params)
    row.parameters_digest = parameters_digest(params)
    row.last_simulated_digest = None
    row.last_simulated_at = None
    row.change_reason = change_reason
    row.lock_version += 1
    db.flush()
    return row


def mark_simulated(db: Session, version_id: int, digest: str | None = None) -> RecommendationStrategyVersion:
    row = db.get(RecommendationStrategyVersion, version_id)
    if not row or row.status != "draft":
        raise ValueError("draft strategy version not found")
    row.last_simulated_digest = digest or row.parameters_digest
    row.last_simulated_at = datetime.now(timezone.utc)
    db.flush()
    return row


def publish_candidate(db: Session, *, version_id: int, operator: str) -> RecommendationStrategyVersion:
    row = db.get(RecommendationStrategyVersion, version_id)
    if not row or row.status != "draft":
        raise ValueError("draft strategy version not found")
    if row.last_simulated_digest != row.parameters_digest:
        raise ValueError("strategy_must_be_simulated_after_last_change")
    row.status = "published"
    row.published_by = operator
    row.published_at = datetime.now(timezone.utc)
    db.flush()
    return row


def select_assignment(
    *,
    release: RecommendationStrategyRelease | None,
    userid: str,
    direction: str,
    kill_switch: bool = False,
) -> tuple[str, int | None]:
    if kill_switch or not release or release.execution_mode == "off":
        return "legacy", None
    if release.candidate_version_id is None:
        return ("stable", release.stable_version_id) if release.stable_version_id else ("legacy", None)
    from app.services.recommendation_scoring_service import bucket_hit
    hit = bucket_hit(release.rollout_percentage, userid, direction, release.candidate_version_id)
    if hit:
        # Shadow computes candidate ranking asynchronously but must never alter
        # the served result.  The request fact records the candidate assignment
        # separately while the serving path remains legacy/stable.
        if release.execution_mode == "shadow":
            return "legacy", None
        return "candidate", release.candidate_version_id
    return ("stable", release.stable_version_id) if release.stable_version_id else ("legacy", None)
