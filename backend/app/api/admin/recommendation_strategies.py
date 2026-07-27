"""推荐策略控制台 API（方案 §11.7）。

三组路由共用一个聚合 router：

- ``/admin/recommendation-strategies``      版本与 release 治理
- ``/admin/recommendation-runtime-control`` 总开关（§7.5 动态控制面）
- ``/admin/recommendation-roles``           控制台 RBAC（§9.10 / §14.8）

所有写操作都要求 ``change_reason``、``lock_version`` CAS、写 audit_log，并在
同一事务插入不可变 ``recommendation_release_history``（kill switch 除外）。
"""
from __future__ import annotations

from typing import Any

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
    AdminRoleAssignRequest,
    AdminUserCreateRequest,
    RecommendationPromoteRequest,
    RecommendationPublishCandidateRequest,
    RecommendationReleaseUpdate,
    RecommendationRollbackRequest,
    RecommendationRuntimeControlUpdate,
    RecommendationSimulationRequest,
    RecommendationStrategyDraftUpdate,
)
from app.services import admin_log_service, admin_user_service
from app.services import recommendation_strategy_service as strategy_service
from app.services.recommendation_strategy_service import (
    ReleaseLockConflict,
    ReleaseStateError,
    create_draft,
    ensure_initial_release,
    mark_simulated,
    publish_candidate,
    release_snapshot as _release_dict,
    update_draft,
)

router = APIRouter()
strategy_router = APIRouter(prefix="/admin/recommendation-strategies", tags=["admin-recommendation"])
runtime_router = APIRouter(prefix="/admin/recommendation-runtime-control", tags=["admin-recommendation"])
roles_router = APIRouter(prefix="/admin/recommendation-roles", tags=["admin-recommendation"])

_VIEWER = ("viewer", "operator", "super_admin")
_OPERATOR = ("operator", "super_admin")
_SUPER = ("super_admin",)


def _require_direction(direction: str) -> str:
    if direction not in strategy_service.DIRECTIONS:
        raise BusinessException(40401, "推荐方向不存在")
    return direction


def _default_release_view(direction: str) -> dict[str, Any]:
    """§7.2: 未初始化的方向语义上就是 off + legacy，展示它不需要先写库。"""
    return {
        "direction": direction,
        "execution_mode": "off",
        "stable_version_id": None,
        "candidate_version_id": None,
        "rollout_percentage": 0,
        "revision": 0,
        "lock_version": 0,
        "updated_by": None,
        "updated_at": None,
        "initialized": False,
    }


def _release_view(db: Session, direction: str) -> dict[str, Any]:
    snapshot = _release_dict(db.get(RecommendationStrategyRelease, direction))
    if snapshot is None:
        return _default_release_view(direction)
    snapshot["initialized"] = True
    return snapshot


def _bootstrap(db: Session, current: AdminUser) -> None:
    """Create the legacy baseline lazily — and never from a viewer's GET.

    §11.7 read endpoints must stay read-only: ``ensure_initial_release`` +
    ``commit`` used to run unconditionally inside every GET (P2-23).  Viewers now
    read the synthesized off/legacy default instead of writing it.
    """
    if not admin_user_service.role_at_least(current, "operator"):
        return
    if ensure_initial_release(db, updated_by=current.username):
        db.commit()
    else:
        db.rollback()


def _version_dict(row: RecommendationStrategyVersion) -> dict[str, Any]:
    return {
        "id": row.id,
        "direction": row.direction,
        "version_no": row.version_no,
        "template_key": row.template_key,
        "status": row.status,
        "parameters": row.parameters,
        "parameters_digest": row.parameters_digest,
        "lock_version": row.lock_version,
        "last_simulated_digest": row.last_simulated_digest,
        "last_simulated_at": row.last_simulated_at,
        "algorithm_version": row.algorithm_version,
        "base_version_id": row.base_version_id,
        "change_reason": row.change_reason,
        "created_by": row.created_by,
        "created_at": row.created_at,
        "published_by": row.published_by,
        "published_at": row.published_at,
    }


def _versions_for(db: Session, direction: str) -> list[dict[str, Any]]:
    rows = (
        db.query(RecommendationStrategyVersion)
        .filter(RecommendationStrategyVersion.direction == direction)
        .order_by(RecommendationStrategyVersion.version_no.desc())
        .all()
    )
    return [_version_dict(row) for row in rows]


def _translate(exc: Exception) -> BusinessException:
    if isinstance(exc, ReleaseLockConflict):
        return BusinessException(40902, "release 已被其他管理员修改")
    return BusinessException(40905, str(exc))


# ---------------------------------------------------------------------------
# 查看
# ---------------------------------------------------------------------------

@strategy_router.get("")
def list_strategies(
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin_role(*_VIEWER)),
):
    _bootstrap(db, current)
    return ok({
        direction: _release_view(db, direction)
        for direction in strategy_service.DIRECTIONS
    })


@strategy_router.get("/{direction}")
def get_strategy(
    direction: str,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin_role(*_VIEWER)),
):
    _require_direction(direction)
    _bootstrap(db, current)
    return ok({
        "release": _release_view(db, direction),
        "versions": _versions_for(db, direction),
    })


@strategy_router.get("/{direction}/versions")
def list_strategy_versions(
    direction: str,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin_role(*_VIEWER)),
):
    _require_direction(direction)
    return ok({"direction": direction, "versions": _versions_for(db, direction)})


@strategy_router.get("/{direction}/history")
def list_release_history(
    direction: str,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin_role(*_VIEWER)),
):
    """不可变历史是回滚的恢复真源（§9.3），后台需要它来选 target_revision。"""
    _require_direction(direction)
    rows = (
        db.query(RecommendationReleaseHistory)
        .filter(RecommendationReleaseHistory.direction == direction)
        .order_by(RecommendationReleaseHistory.revision.desc())
        .all()
    )
    return ok({
        "direction": direction,
        "history": [
            {
                "revision": row.revision,
                "operation": row.operation,
                "execution_mode": row.execution_mode,
                "stable_version_id": row.stable_version_id,
                "candidate_version_id": row.candidate_version_id,
                "rollout_percentage": row.rollout_percentage,
                "target_revision": row.target_revision,
                "change_reason": row.change_reason,
                "created_by": row.created_by,
                "created_at": row.created_at,
            }
            for row in rows
        ],
    })


# ---------------------------------------------------------------------------
# 草稿
# ---------------------------------------------------------------------------

@strategy_router.post("/{direction}/drafts")
def create_strategy_draft(
    direction: str,
    req: RecommendationStrategyDraftUpdate,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin_role(*_OPERATOR)),
):
    _require_direction(direction)
    try:
        row = create_draft(
            db,
            direction=direction,
            template_key=req.template_key,
            parameters=req.parameters,
            created_by=current.username,
            change_reason=req.change_reason,
        )
    except (ReleaseStateError, ValueError) as exc:
        db.rollback()
        raise _translate(exc) from exc
    db.commit()
    return ok({
        "id": row.id, "version_no": row.version_no,
        "lock_version": row.lock_version, "parameters_digest": row.parameters_digest,
    })


@strategy_router.put("/drafts/{version_id}")
def edit_strategy_draft(
    version_id: int,
    req: RecommendationStrategyDraftUpdate,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin_role(*_OPERATOR)),
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
    except ValueError as exc:
        db.rollback()
        raise BusinessException(40401, str(exc)) from exc
    db.commit()
    return ok({"id": row.id, "lock_version": row.lock_version, "parameters_digest": row.parameters_digest})


# ---------------------------------------------------------------------------
# 模拟（§8）
# ---------------------------------------------------------------------------

_JOB_SUMMARY_FIELDS = (
    "city", "district", "job_category", "salary_floor_monthly",
    "salary_ceiling_monthly", "pay_type", "employment_type", "company", "created_at",
)
_RESUME_SUMMARY_FIELDS = (
    "expected_cities", "expected_districts", "expected_job_categories",
    "salary_expect_floor_monthly", "education", "work_experience", "age",
    "gender", "created_at",
)


def _candidate_summary(candidate: dict, direction: str) -> dict[str, Any]:
    """后台可见摘要（§8.2）；刻意不含手机号/联系人/自由文本描述。"""
    fields = _JOB_SUMMARY_FIELDS if direction == "search_job" else _RESUME_SUMMARY_FIELDS
    summary = {key: candidate.get(key) for key in fields}
    summary["id"] = candidate.get("id")
    summary["owner_userid"] = candidate.get("owner_userid")
    return summary


def _component_reason_codes(current_detail, draft_detail) -> list[str]:
    """把"为什么升降位"落到具体分项，而不是只给一个方向。"""
    if current_detail is None or draft_detail is None:
        return []
    codes: list[str] = []
    comparisons = (
        ("match_score", "match"),
        ("quality_score", "quality"),
        ("freshness_score", "freshness"),
        ("exposure_opportunity", "exposure_opportunity"),
        ("base_score", "base_score"),
    )
    for attr, label in comparisons:
        delta = getattr(draft_detail, attr) - getattr(current_detail, attr)
        if delta > 1e-9:
            codes.append(f"{label}_up")
        elif delta < -1e-9:
            codes.append(f"{label}_down")
    repeat_delta = draft_detail.repeat_factor - current_detail.repeat_factor
    if repeat_delta < -1e-9:
        codes.append("repeat_penalty_stronger")
    elif repeat_delta > 1e-9:
        codes.append("repeat_penalty_weaker")
    if draft_detail.is_exploration and not current_detail.is_exploration:
        codes.append("exploration_slot_gained")
    elif current_detail.is_exploration and not draft_detail.is_exploration:
        codes.append("exploration_slot_lost")
    return codes


def rank_change_reasons(current_items: list, draft_items: list) -> list[dict[str, Any]]:
    """§8.2 "排名变化原因码"：逐候选对比线上侧与草稿侧。"""
    current_by_id = {item.target_id: item for item in current_items}
    draft_by_id = {item.target_id: item for item in draft_items}
    changes: list[dict[str, Any]] = []
    for target_id in list(draft_by_id) + [i for i in current_by_id if i not in draft_by_id]:
        before = current_by_id.get(target_id)
        after = draft_by_id.get(target_id)
        if before is None:
            movement, codes = "entered", ["entered_top_n"]
        elif after is None:
            movement, codes = "left", ["left_top_n"]
        elif after.position < before.position:
            movement, codes = "up", ["rank_up"]
        elif after.position > before.position:
            movement, codes = "down", ["rank_down"]
        else:
            movement, codes = "unchanged", []
        if before is not None and after is not None:
            codes = codes + _component_reason_codes(before.score_detail, after.score_detail)
        if after is not None:
            codes = codes + [code for code in after.reason_codes if code not in codes]
        changes.append({
            "target_id": target_id,
            "current_position": before.position if before else None,
            "draft_position": after.position if after else None,
            "movement": movement,
            "reason_codes": codes,
        })
    changes.sort(key=lambda row: (row["draft_position"] is None, row["draft_position"] or 0))
    return changes


def _legacy_baseline_items(candidates: list[dict], direction: str, top_n: int):
    """Compatibility wrapper for callers/tests that used the former API helper."""
    from app.services.recommendation_simulation_service import _legacy_baseline

    return _legacy_baseline(candidates, direction, top_n)


@strategy_router.post("/drafts/{version_id}/simulate")
def simulate_strategy_draft(
    version_id: int,
    req: RecommendationSimulationRequest,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin_role(*_VIEWER)),
):
    """真实模拟：与线上共享精排 LLM、曝光数据和确定性排序流水线。

    §8.3 无副作用：本端点只读事实表（曝光/重复曝光），不写快照、不改
    ``shown_items``、不写曝光、不改分桶、不写对话日志、不发企微。唯一写入是
    §7.1 要求的 ``last_simulated_digest``，它是发布前置校验的一部分。
    """
    row = db.get(RecommendationStrategyVersion, version_id)
    if not row or row.status != "draft":
        raise BusinessException(40401, "草稿不存在")
    _require_direction(req.direction)
    if row.direction != req.direction:
        raise BusinessException(40905, "strategy direction mismatch")

    from app.services.recommendation_simulation_service import simulate_strategy

    simulation = simulate_strategy(
        db,
        draft=row,
        direction=req.direction,
        user_id=req.user_id,
        raw_query=req.raw_query,
        criteria=req.criteria,
    )
    if simulation.llm_invoked and simulation.semantic_source != "llm":
        db.rollback()
        raise BusinessException(
            50101,
            "LLM recommendation simulation failed; the draft was not marked as simulated",
        )

    mark_simulated(db, version_id)
    db.commit()

    summaries = {
        str(item.get("id")): _candidate_summary(item, req.direction)
        for item in simulation.candidates
    }
    return ok({
        "version_id": version_id,
        "direction": req.direction,
        "parameters_digest": row.parameters_digest,
        "side_effects_written": False,
        "call_site": "recommendation_simulation",
        "llm_invoked": simulation.llm_invoked,
        "semantic_source": simulation.semantic_source,
        "simulation_mode": simulation.simulation_mode,
        "llm_input_tokens": simulation.llm_input_tokens,
        "llm_output_tokens": simulation.llm_output_tokens,
        "current_basis": simulation.current_basis,
        "exposure_available": simulation.exposure_available,
        "rotation_date": simulation.rotation_date,
        "current": [
            item.model_dump(mode="json") for item in simulation.current_items
        ],
        "draft": [
            item.model_dump(mode="json") for item in simulation.draft_items
        ],
        "rank_changes": rank_change_reasons(
            simulation.current_items,
            simulation.draft_items,
        ),
        "candidate_summaries": summaries,
        "candidate_count": len(simulation.candidates),
    })


# ---------------------------------------------------------------------------
# 发布 / 灰度 / 全量 / 回滚
# ---------------------------------------------------------------------------

@strategy_router.post("/drafts/{version_id}/publish-candidate")
def publish_strategy_candidate(
    version_id: int,
    req: RecommendationPublishCandidateRequest,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin_role(*_SUPER)),
):
    row = db.get(RecommendationStrategyVersion, version_id)
    if not row or row.status != "draft":
        raise BusinessException(40401, "草稿不存在")
    before = _version_dict(row)
    direction = row.direction
    try:
        row = publish_candidate(
            db, version_id=version_id, operator=current.username,
            change_reason=req.change_reason, lock_version=req.lock_version,
        )
        release, release_before = strategy_service.record_candidate_publication(
            db, direction=direction, version_id=version_id,
            lock_version=req.release_lock_version, change_reason=req.change_reason,
            operator=current.username,
        )
    except RuntimeError as exc:
        db.rollback()
        raise BusinessException(40902, str(exc)) from exc
    except (ReleaseStateError, ValueError) as exc:
        db.rollback()
        raise _translate(exc) from exc
    admin_log_service.write_admin_log(
        db, target_type="recommendation_strategy", target_id=version_id,
        action="strategy_publish", operator=current.username,
        reason=req.change_reason,
        before={"version": before, "release": release_before},
        after={"version": _version_dict(row), "release": _release_dict(release)},
    )
    db.commit()
    return ok({
        "version_id": row.id, "status": row.status,
        "lock_version": row.lock_version, "release": _release_dict(release),
    })


@strategy_router.put("/{direction}/release")
def update_release(
    direction: str,
    req: RecommendationReleaseUpdate,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin_role(*_SUPER)),
):
    _require_direction(direction)
    try:
        row, before = strategy_service.update_release(
            db, direction=direction, execution_mode=req.execution_mode,
            candidate_version_id=req.candidate_version_id,
            rollout_percentage=req.rollout_percentage, lock_version=req.lock_version,
            change_reason=req.change_reason, operator=current.username,
        )
    except ReleaseLockConflict as exc:
        db.rollback()
        raise BusinessException(40902, "release 已被其他管理员修改") from exc
    except ReleaseStateError as exc:
        db.rollback()
        raise BusinessException(40101, str(exc)) from exc
    admin_log_service.write_admin_log(
        db, target_type="recommendation_strategy", target_id=direction,
        action="strategy_rollout", operator=current.username, reason=req.change_reason,
        before=before, after=_release_dict(row),
    )
    db.commit()
    return ok(_release_dict(row))


@strategy_router.post("/{direction}/promote")
def promote_release(
    direction: str,
    req: RecommendationPromoteRequest,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin_role(*_SUPER)),
):
    _require_direction(direction)
    try:
        row, before, archived_version_id = strategy_service.promote_release(
            db, direction=direction, lock_version=req.lock_version,
            change_reason=req.change_reason, operator=current.username,
        )
    except ReleaseLockConflict as exc:
        db.rollback()
        raise BusinessException(40902, "release 已被其他管理员修改") from exc
    except ReleaseStateError as exc:
        db.rollback()
        raise BusinessException(40905, str(exc)) from exc
    after = _release_dict(row) or {}
    after["archived_version_id"] = archived_version_id
    admin_log_service.write_admin_log(
        db, target_type="recommendation_strategy", target_id=direction,
        action="strategy_promote", operator=current.username, reason=req.change_reason,
        before=before, after=after,
    )
    db.commit()
    return ok(after)


@strategy_router.post("/{direction}/rollback")
def rollback_release(
    direction: str,
    req: RecommendationRollbackRequest,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin_role(*_SUPER)),
):
    _require_direction(direction)
    try:
        row, before = strategy_service.rollback_release(
            db, direction=direction, target_revision=req.target_revision,
            lock_version=req.lock_version, change_reason=req.change_reason,
            operator=current.username,
        )
    except ReleaseLockConflict as exc:
        db.rollback()
        raise BusinessException(40902, "release 已被其他管理员修改") from exc
    except ReleaseStateError as exc:
        db.rollback()
        raise BusinessException(40401, str(exc)) from exc
    admin_log_service.write_admin_log(
        db, target_type="recommendation_strategy", target_id=direction,
        action="strategy_rollback", operator=current.username, reason=req.change_reason,
        before=before, after=_release_dict(row),
    )
    db.commit()
    return ok(_release_dict(row))


# ---------------------------------------------------------------------------
# 总开关（§7.5）
# ---------------------------------------------------------------------------

def _control_dict(row: RecommendationRuntimeControl | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "scope": row.scope,
        "kill_switch": bool(row.kill_switch),
        "revision": int(row.revision),
        "lock_version": int(row.lock_version),
        "change_reason": row.change_reason,
        "updated_by": row.updated_by,
        "updated_at": row.updated_at,
    }


@runtime_router.get("")
def get_runtime_control(
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin_role(*_VIEWER)),
):
    """DB 侧真值 + 本进程当前生效值，便于确认分发是否收敛。"""
    row = db.get(RecommendationRuntimeControl, "global")
    local = strategy_service.runtime_control_state()
    return ok({
        "control": _control_dict(row),
        "env_override": strategy_service.env_kill_switch_override(),
        "local": None if local is None else {
            "kill_switch": local.kill_switch,
            "revision": local.revision,
            "source": local.source,
            "age_seconds": round(local.age_seconds(), 3),
            "fresh": local.is_fresh(),
        },
        "max_propagation_seconds": strategy_service.RUNTIME_CONTROL_MAX_AGE_SECONDS,
    })


@runtime_router.put("/kill-switch")
def update_kill_switch(
    req: RecommendationRuntimeControlUpdate,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin_role(*_SUPER)),
):
    """先提交 DB revision，再 write-through Redis 并发布 Pub/Sub（§7.5）。"""
    try:
        row, before = strategy_service.set_kill_switch(
            db, enabled=req.enabled, lock_version=req.lock_version,
            change_reason=req.change_reason, operator=current.username,
        )
    except ReleaseLockConflict as exc:
        db.rollback()
        raise BusinessException(40902, "kill switch 已被其他管理员修改") from exc
    admin_log_service.write_admin_log(
        db, target_type="recommendation_strategy", target_id="global",
        action="strategy_kill_switch", operator=current.username,
        reason=req.change_reason,
        before=before,
        after={"kill_switch": bool(req.enabled), "revision": int(row.revision)},
    )
    db.commit()
    kill_switch, revision = bool(row.kill_switch), int(row.revision)
    # Redis 只是提交之后的加速通道，失败不回滚：所有进程最迟 5 秒后由 DB 轮询收敛。
    broadcast_ok = strategy_service.broadcast_runtime_control(
        kill_switch=kill_switch, revision=revision,
    )
    return ok({
        "kill_switch": kill_switch,
        "revision": revision,
        "lock_version": int(row.lock_version),
        "broadcast": broadcast_ok,
        "max_propagation_seconds": strategy_service.RUNTIME_CONTROL_MAX_AGE_SECONDS,
    })


# ---------------------------------------------------------------------------
# 控制台 RBAC（§9.10 / §14.8）
# ---------------------------------------------------------------------------

@roles_router.get("/me")
def my_role(current: AdminUser = Depends(require_admin_role(*_VIEWER))):
    role = admin_user_service.resolve_role(current)
    return ok({
        "username": current.username,
        "role": role,
        "permissions": admin_user_service.permissions_for(role),
    })


@roles_router.get("/matrix")
def role_matrix(_: AdminUser = Depends(require_admin_role(*_VIEWER))):
    return ok({
        "roles": list(admin_user_service.ADMIN_ROLES),
        "default_role": admin_user_service.DEFAULT_ADMIN_ROLE,
        "permissions": {
            name: list(roles) for name, roles in admin_user_service.ROLE_PERMISSIONS.items()
        },
    })


@roles_router.get("/admins")
def list_admin_users(
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin_role(*_SUPER)),
):
    return ok(admin_user_service.summarize_all(admin_user_service.list_admins(db)))


@roles_router.post("/admins")
def create_admin_user(
    req: AdminUserCreateRequest,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin_role(*_SUPER)),
):
    try:
        row = admin_user_service.create_admin(
            db, username=req.username, password=req.password,
            role=req.role, display_name=req.display_name,
        )
    except ValueError as exc:
        db.rollback()
        raise BusinessException(40101, str(exc)) from exc
    admin_log_service.write_admin_log(
        db, target_type="user", target_id=row.id, action="manual_edit",
        operator=current.username, reason=req.change_reason,
        before=None, after={"admin_user_created": row.username, "role": req.role},
    )
    db.commit()
    return ok(admin_user_service.admin_summary(row))


@roles_router.put("/admins/{admin_id}/role")
def set_admin_role(
    admin_id: int,
    req: AdminRoleAssignRequest,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin_role(*_SUPER)),
):
    target = admin_user_service.get_by_id(db, admin_id)
    if not target:
        raise BusinessException(40401, "管理员不存在")
    before = admin_user_service.resolve_role(target)
    if before == "super_admin" and req.role != "super_admin":
        # 把最后一个可用 super_admin 降权会让发布/回滚/kill 永久不可达。
        if admin_user_service.count_enabled_super_admins(db, exclude_id=admin_id) == 0:
            raise BusinessException(40101, "至少保留一个可用的 super_admin")
    try:
        admin_user_service.set_role(db, target, req.role)
    except ValueError as exc:
        db.rollback()
        raise BusinessException(40101, str(exc)) from exc
    admin_log_service.write_admin_log(
        db, target_type="user", target_id=admin_id, action="manual_edit",
        operator=current.username, reason=req.change_reason,
        before={"role": before}, after={"role": req.role},
    )
    db.commit()
    return ok(admin_user_service.admin_summary(target))


router.include_router(strategy_router)
router.include_router(runtime_router)
router.include_router(roles_router)
