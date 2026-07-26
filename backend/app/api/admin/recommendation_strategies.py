from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin_role
from app.core.exceptions import BusinessException
from app.core.responses import ok
from app.models import (
    AdminUser,
    RecommendationReleaseHistory,
    RecommendationRuntimeControl,
    RecommendationStrategyRelease,
    RecommendationStrategyVersion,
)
from app.schemas.recommendation import (
    RecommendationReleaseUpdate,
    RecommendationPromoteRequest,
    RecommendationRollbackRequest,
    RecommendationRuntimeControlUpdate,
    RecommendationSimulationRequest,
    RecommendationStrategyDraftUpdate,
    RecommendationStrategyParameters,
)
from app.services import admin_log_service
from app.services.recommendation_strategy_service import (
    canonical_parameters,
    create_draft,
    ensure_initial_release,
    mark_simulated,
    publish_candidate,
    update_draft,
)

router = APIRouter(prefix="/admin/recommendation-strategies", tags=["admin-recommendation"])


def _release_dict(row: RecommendationStrategyRelease | None) -> dict | None:
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


@router.get("")
def list_strategies(
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin_role("viewer", "operator", "super_admin")),
):
    ensure_initial_release(db)
    db.commit()
    return ok({
        direction: _release_dict(db.get(RecommendationStrategyRelease, direction))
        for direction in ("search_job", "search_worker")
    })


@router.get("/{direction}")
def get_strategy(
    direction: str,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin_role("viewer", "operator", "super_admin")),
):
    if direction not in ("search_job", "search_worker"):
        raise BusinessException(40401, "推荐方向不存在")
    ensure_initial_release(db)
    db.commit()
    versions = (
        db.query(RecommendationStrategyVersion)
        .filter(RecommendationStrategyVersion.direction == direction)
        .order_by(RecommendationStrategyVersion.version_no.desc())
        .all()
    )
    return ok({
        "release": _release_dict(db.get(RecommendationStrategyRelease, direction)),
        "versions": [
            {
                "id": row.id, "version_no": row.version_no, "template_key": row.template_key,
                "status": row.status, "parameters": row.parameters,
                "parameters_digest": row.parameters_digest, "lock_version": row.lock_version,
                "last_simulated_digest": row.last_simulated_digest,
                "change_reason": row.change_reason, "created_by": row.created_by,
                "created_at": row.created_at,
            }
            for row in versions
        ],
    })


@router.post("/{direction}/drafts")
def create_strategy_draft(
    direction: str,
    req: RecommendationStrategyDraftUpdate,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin_role("operator", "super_admin")),
):
    row = create_draft(
        db,
        direction=direction,
        template_key=req.template_key,
        parameters=req.parameters,
        created_by=current.username,
        change_reason=req.change_reason,
    )
    db.commit()
    return ok({"id": row.id, "version_no": row.version_no, "parameters_digest": row.parameters_digest})


@router.put("/drafts/{version_id}")
def edit_strategy_draft(
    version_id: int,
    req: RecommendationStrategyDraftUpdate,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin_role("operator", "super_admin")),
):
    try:
        row = update_draft(
            db, version_id=version_id, lock_version=req.lock_version,
            template_key=req.template_key, parameters=req.parameters,
            change_reason=req.change_reason,
        )
    except RuntimeError as exc:
        db.rollback()
        raise BusinessException(40902, str(exc)) from exc
    db.commit()
    return ok({"id": row.id, "lock_version": row.lock_version, "parameters_digest": row.parameters_digest})


@router.post("/drafts/{version_id}/simulate")
def simulate_strategy_draft(
    version_id: int,
    req: RecommendationSimulationRequest,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin_role("viewer", "operator", "super_admin")),
):
    row = db.get(RecommendationStrategyVersion, version_id)
    if not row or row.status != "draft":
        raise BusinessException(40401, "草稿不存在")
    if row.direction != req.direction:
        raise BusinessException(40905, "strategy direction mismatch")
    from app.services import search_service
    from app.services.recommendation_request_service import rank_candidate_dicts
    if req.direction == "search_job":
        candidates = search_service._jobs_to_dicts(
            search_service._query_jobs(req.criteria, 50, db), db,
        )
    else:
        candidates = search_service._resumes_to_dicts(
            search_service._query_resumes(req.criteria, 50, db),
        )
    query_digest = search_service.conversation_service.compute_query_digest(req.criteria)
    _draft_ranked, draft_items = rank_candidate_dicts(
        candidates,
        direction=req.direction,
        criteria=req.criteria,
        userid=req.user_id or "simulation",
        query_digest=query_digest,
        strategy_version=row.id,
        parameters=row.parameters,
        semantic_ranked_items=[],
        rotation_date=datetime.now(timezone.utc).date().isoformat(),
    )
    release = db.get(RecommendationStrategyRelease, req.direction)
    current_items = []
    current_version = (
        db.get(RecommendationStrategyVersion, release.stable_version_id)
        if release and release.stable_version_id else None
    )
    if current_version:
        _current_ranked, current_items = rank_candidate_dicts(
            candidates,
            direction=req.direction,
            criteria=req.criteria,
            userid=req.user_id or "simulation",
            query_digest=query_digest,
            strategy_version=current_version.id,
            parameters=current_version.parameters,
            semantic_ranked_items=[],
            rotation_date=datetime.now(timezone.utc).date().isoformat(),
        )
    mark_simulated(db, version_id)
    db.commit()
    return ok({
        "version_id": version_id,
        "parameters_digest": row.parameters_digest,
        "side_effects_written": False,
        "call_site": "recommendation_simulation",
        "direction": req.direction,
        "current": [item.model_dump(mode="json") for item in current_items],
        "draft": [item.model_dump(mode="json") for item in draft_items],
        "candidate_count": len(candidates),
    })


@router.post("/drafts/{version_id}/publish-candidate")
def publish_strategy_candidate(
    version_id: int,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin_role("super_admin")),
):
    try:
        row = publish_candidate(db, version_id=version_id, operator=current.username)
    except ValueError as exc:
        db.rollback()
        raise BusinessException(40904, str(exc)) from exc
    admin_log_service.write_admin_log(
        db, target_type="recommendation_strategy", target_id=version_id,
        action="strategy_publish", operator=current.username,
        reason=row.change_reason, after={"status": "published", "version_id": version_id},
    )
    db.commit()
    return ok({"version_id": row.id, "status": row.status})


@router.put("/{direction}/release")
def update_release(
    direction: str,
    req: RecommendationReleaseUpdate,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin_role("super_admin")),
):
    row = db.get(RecommendationStrategyRelease, direction)
    if not row:
        ensure_initial_release(db, updated_by=current.username)
        row = db.get(RecommendationStrategyRelease, direction)
    if row.lock_version != req.lock_version:
        raise BusinessException(40902, "release 已被其他管理员修改")
    if req.execution_mode != "off" and req.candidate_version_id is None:
        raise BusinessException(40101, "shadow/on 必须指定候选版本")
    if req.candidate_version_id is not None:
        candidate = db.get(RecommendationStrategyVersion, req.candidate_version_id)
        if not candidate or candidate.status != "published" or candidate.direction != direction:
            raise BusinessException(40905, "candidate version must be published and match direction")
    before = _release_dict(row)
    row.execution_mode = req.execution_mode
    row.candidate_version_id = req.candidate_version_id
    row.rollout_percentage = req.rollout_percentage
    row.revision += 1
    row.lock_version += 1
    row.updated_by = current.username
    db.add(RecommendationReleaseHistory(
        direction=direction, revision=row.revision, operation="mode_change",
        execution_mode=row.execution_mode, stable_version_id=row.stable_version_id,
        candidate_version_id=row.candidate_version_id, rollout_percentage=row.rollout_percentage,
        change_reason=req.change_reason, created_by=current.username,
    ))
    admin_log_service.write_admin_log(
        db, target_type="recommendation_strategy", target_id=direction,
        action="strategy_rollout", operator=current.username, reason=req.change_reason,
        before=before, after=_release_dict(row),
    )
    db.commit()
    return ok(_release_dict(row))


@router.post("/{direction}/promote")
def promote_release(
    direction: str,
    req: RecommendationPromoteRequest,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin_role("super_admin")),
):
    row = db.get(RecommendationStrategyRelease, direction)
    if not row or not row.candidate_version_id:
        raise BusinessException(40905, "no candidate version to promote")
    if row.lock_version != req.lock_version:
        raise BusinessException(40902, "release has been modified")
    candidate = db.get(RecommendationStrategyVersion, row.candidate_version_id)
    if not candidate or candidate.status != "published" or candidate.direction != direction:
        raise BusinessException(40905, "invalid candidate version")
    before = _release_dict(row)
    row.stable_version_id = row.candidate_version_id
    row.candidate_version_id = None
    row.execution_mode = "on"
    row.rollout_percentage = 0
    row.revision += 1
    row.lock_version += 1
    row.updated_by = current.username
    db.add(RecommendationReleaseHistory(
        direction=direction, revision=row.revision, operation="promote",
        execution_mode=row.execution_mode, stable_version_id=row.stable_version_id,
        candidate_version_id=None, rollout_percentage=0,
        change_reason=req.change_reason, created_by=current.username,
    ))
    admin_log_service.write_admin_log(
        db, target_type="recommendation_strategy", target_id=direction,
        action="strategy_promote", operator=current.username, reason=req.change_reason,
        before=before, after=_release_dict(row),
    )
    db.commit()
    return ok(_release_dict(row))


@router.post("/{direction}/rollback")
def rollback_release(
    direction: str,
    req: RecommendationRollbackRequest,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin_role("super_admin")),
):
    row = db.get(RecommendationStrategyRelease, direction)
    target = db.query(RecommendationReleaseHistory).filter_by(
        direction=direction, revision=req.target_revision
    ).first()
    if not row or not target:
        raise BusinessException(40401, "回滚目标不存在")
    if row.lock_version != req.lock_version:
        raise BusinessException(40902, "release 已被其他管理员修改")
    row.execution_mode = target.execution_mode
    row.stable_version_id = target.stable_version_id
    row.candidate_version_id = target.candidate_version_id
    row.rollout_percentage = target.rollout_percentage
    row.revision += 1
    row.lock_version += 1
    row.updated_by = current.username
    db.add(RecommendationReleaseHistory(
        direction=direction, revision=row.revision, operation="rollback",
        execution_mode=row.execution_mode, stable_version_id=row.stable_version_id,
        candidate_version_id=row.candidate_version_id, rollout_percentage=row.rollout_percentage,
        target_revision=req.target_revision, change_reason=req.change_reason,
        created_by=current.username,
    ))
    admin_log_service.write_admin_log(
        db, target_type="recommendation_strategy", target_id=direction,
        action="strategy_rollback", operator=current.username, reason=req.change_reason,
        after=_release_dict(row),
    )
    db.commit()
    return ok(_release_dict(row))


@router.put("/runtime-control/kill-switch")
def update_kill_switch(
    req: RecommendationRuntimeControlUpdate,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin_role("super_admin")),
):
    row = db.get(RecommendationRuntimeControl, "global")
    if not row:
        row = RecommendationRuntimeControl(scope="global", kill_switch=False, revision=1, lock_version=1, updated_by="system", change_reason="initial")
        db.add(row)
        db.flush()
    if row.lock_version != req.lock_version:
        raise BusinessException(40902, "kill switch 已被其他管理员修改")
    row.kill_switch = req.enabled
    row.revision += 1
    row.lock_version += 1
    row.change_reason = req.change_reason
    row.updated_by = current.username
    admin_log_service.write_admin_log(
        db, target_type="recommendation_strategy", target_id="global",
        action="strategy_kill_switch", operator=current.username,
        reason=req.change_reason, after={"kill_switch": req.enabled, "revision": row.revision},
    )
    db.commit()
    return ok({"kill_switch": bool(row.kill_switch), "revision": row.revision, "lock_version": row.lock_version})
