"""Runtime strategy assignment and immutable snapshot helpers."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from app.models import RecommendationRuntimeControl, RecommendationStrategyRelease, RecommendationStrategyVersion
from app.schemas.recommendation import StrategyAssignment
from app.services.recommendation_scoring_service import bucket_hit
from app.services.recommendation_strategy_service import select_assignment


@dataclass(frozen=True)
class AssignmentDecision:
    assignment: StrategyAssignment
    version: RecommendationStrategyVersion | None
    snapshot_id: str
    request_id: str


def choose_assignment(
    db: Session,
    *,
    userid: str,
    direction: str,
    request_id: str | None = None,
) -> AssignmentDecision:
    release = db.get(RecommendationStrategyRelease, direction)
    control = db.get(RecommendationRuntimeControl, "global")
    kill_switch = bool(control.kill_switch) if control else False
    assignment, version_id = select_assignment(
        release=release,
        userid=userid,
        direction=direction,
        kill_switch=kill_switch,
    )
    version = db.get(RecommendationStrategyVersion, version_id) if version_id else None
    execution_mode = release.execution_mode if release and not kill_switch else "off"
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

