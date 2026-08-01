"""Runtime strategy assignment and immutable snapshot helpers."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from app.models import RecommendationStrategyRelease, RecommendationStrategyVersion
from app.schemas.recommendation import StrategyAssignment
from app.services.recommendation_scoring_service import bucket_hit
from app.services.recommendation_strategy_service import (
    load_published_version,
    resolve_runtime_control,
    select_assignment,
    shadow_candidate_version_id,
    snapshot_is_invalidated_by_kill_switch,
)


@dataclass(frozen=True)
class AssignmentDecision:
    assignment: StrategyAssignment
    version: RecommendationStrategyVersion | None
    snapshot_id: str
    request_id: str
    #: Version the shadow executor must double-compute for, or None when no
    #: shadow work is due for this request (§7.5).  The served reply never waits
    #: for it and never uses its result.
    shadow_version_id: int | None = None
    #: Runtime control revision the decision was made under; recorded so the
    #: request fact can prove which kill-switch generation served the reply.
    control_revision: int = 0
    kill_switch: bool = False


def choose_assignment(
    db: Session,
    *,
    userid: str,
    direction: str,
    request_id: str | None = None,
) -> AssignmentDecision:
    release = db.get(RecommendationStrategyRelease, direction)
    # §7.5: the kill switch is read through the runtime-control resolver, which
    # serves a ≤5s process-local value, re-sources from DB/Redis when stale and
    # fails safe to off/legacy when neither is reachable.
    control = resolve_runtime_control(db)
    kill_switch = control.kill_switch
    assignment, version_id = select_assignment(
        release=release,
        userid=userid,
        direction=direction,
        kill_switch=kill_switch,
    )
    version = load_published_version(db, version_id)
    execution_mode = release.execution_mode if release and not kill_switch else "off"
    shadow_version_id = shadow_candidate_version_id(
        release=release, userid=userid, direction=direction, kill_switch=kill_switch,
    )
    strategy_assignment = StrategyAssignment(
        direction=direction,
        execution_mode=execution_mode,
        assignment=assignment,
        strategy_version_id=version_id,
        candidate_version_id=release.candidate_version_id if release else None,
        algorithm_version="recommendation-v1" if version_id else "legacy",
        revision=release.revision if release else 0,
    )
    return AssignmentDecision(
        assignment=strategy_assignment,
        version=version,
        snapshot_id=str(uuid.uuid4()),
        request_id=request_id or str(uuid.uuid4()),
        shadow_version_id=shadow_version_id,
        control_revision=control.revision,
        kill_switch=kill_switch,
    )


def snapshot_must_fall_back_to_legacy(
    db: Session,
    *,
    algorithm_version: str | None,
) -> bool:
    """``show_more`` gate: True means the stored v1 snapshot must be dropped.

    §7.5 requires a killed process to stop paging an existing v1 snapshot and
    rebuild with legacy under the original criteria, still excluding the already
    shown IDs.  Kept here so the search layer has one call to make.
    """
    return snapshot_is_invalidated_by_kill_switch(
        algorithm_version=algorithm_version,
        kill_switch=resolve_runtime_control(db).kill_switch,
    )


def exploration_hit(
    *,
    userid: str,
    direction: str,
    query_digest: str,
    strategy_version: str | int,
    rotation_date: date | str,
    percentage: int,
) -> bool:
    return bucket_hit(
        percentage,
        userid,
        direction,
        query_digest,
        strategy_version,
        rotation_date,
    )
