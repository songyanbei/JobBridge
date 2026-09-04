"""后台演示工作区控制面。

演示批次没有复用普通用户删除接口。这个模块只负责控制面生命周期：

* 所有操作都在 development/test 且显式开启 demo mode 时才允许；
* 资源范围来自 ``demo_resource`` 和 workspace 的三个 synthetic principal；
* 清理按固定依赖顺序分阶段提交，失败后可重复执行；
* 不修改真实用户的 ``User.role``，也不触碰 AIBot identity binding。

长期方案会给业务表增加显式 ``demo_id``。在该字段落地前，本模块使用精确的
workspace 资源登记和 synthetic userid 集合做 scope，禁止使用模糊前缀匹配。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import inspect, or_
from sqlalchemy.orm import Session

from app.config import settings
from app.core.redis_client import get_redis
from app.models import (
    ActionExecution,
    ActionParseArtifact,
    AuditLog,
    ContactAccessAudit,
    ContactDelivery,
    ContactGrant,
    ContactRequest,
    ConversationLog,
    DemoPrincipal,
    DemoResource,
    DemoWorkspace,
    DemoWorkspaceMember,
    DomainOutboxEvent,
    EventLog,
    Job,
    JobReplacement,
    MediaAssetLifecycle,
    RecommendationDelivery,
    RecommendationExposureDaily,
    RecommendationImpression,
    RecommendationRequest,
    RecommendationSearchAttempt,
    Resume,
    ResumeReplacement,
    ResumeMediaIsolationIssue,
    TargetCleanupTask,
    User,
    WecomInboundEvent,
    WecomOutboundOutbox,
)
from app.services.admin_log_service import write_admin_log


class DemoAdminError(ValueError):
    """Deterministic, fail-closed control-plane error."""


class DemoAdminNotFound(DemoAdminError):
    pass


class DemoAdminConflict(DemoAdminError):
    pass


_ACTIVE_STATUSES = {"active", "disabled", "cleaning", "failed", "cleaned"}

# Resource types whose target id is a scalar primary key.  The registry is
# intentionally allow-listed: an unknown type is not silently interpreted as
# a table name or SQL fragment.
_EXACT_RESOURCE_MODELS: dict[str, tuple[Any, str]] = {
    "job": (Job, "id"),
    "resume": (Resume, "id"),
    "job_replacement": (JobReplacement, "id"),
    "resume_replacement": (ResumeReplacement, "id"),
    "media_asset_lifecycle": (MediaAssetLifecycle, "id"),
    "target_cleanup_task": (TargetCleanupTask, "id"),
    "conversation_log": (ConversationLog, "id"),
    "event_log": (EventLog, "id"),
    "wecom_inbound_event": (WecomInboundEvent, "id"),
    "wecom_outbound_outbox": (WecomOutboundOutbox, "id"),
    "action_execution": (ActionExecution, "id"),
    "action_parse_artifact": (ActionParseArtifact, "parse_ref"),
    "contact_request": (ContactRequest, "request_id"),
    "contact_grant": (ContactGrant, "grant_id"),
    "contact_delivery": (ContactDelivery, "delivery_id"),
    "contact_access_audit": (ContactAccessAudit, "id"),
    "recommendation_request": (RecommendationRequest, "request_id"),
    "recommendation_search_attempt": (RecommendationSearchAttempt, "attempt_id"),
    "recommendation_delivery": (RecommendationDelivery, "delivery_id"),
    "recommendation_impression": (RecommendationImpression, "id"),
    # Daily exposure rows have no scalar business id. Their exact cleanup
    # scope is the explicit demo_id column; legacy NULL rows stay untouched.
    "recommendation_exposure_daily": (RecommendationExposureDaily, "demo_id"),
    "domain_outbox_event": (DomainOutboxEvent, "id"),
    "resume_media_isolation_issue": (ResumeMediaIsolationIssue, "id"),
    "audit_log": (AuditLog, "id"),
}

# Exact owner/actor scopes derived from synthetic principals.  These are not
# prefix scans: only the three userid values owned by the target workspace are
# used.
_USER_SCOPED_MODELS: tuple[tuple[Any, str], ...] = (
    (Job, "owner_userid"),
    (Resume, "owner_userid"),
    (MediaAssetLifecycle, "owner_userid"),
    (ConversationLog, "userid"),
    (EventLog, "userid"),
    (WecomInboundEvent, "from_userid"),
    (WecomOutboundOutbox, "userid"),
    (ActionExecution, "actor_userid"),
    (ActionParseArtifact, "actor_userid"),
    (RecommendationRequest, "viewer_userid"),
    (RecommendationDelivery, "userid"),
    (RecommendationImpression, "viewer_userid"),
    (ContactRequest, "actor_id"),
    (ContactGrant, "actor_id"),
    (ContactDelivery, "actor_id"),
    (ContactAccessAudit, "actor_hash"),
)

# Child rows must be removed before recommendation requests and fact sources.
_CLEANUP_ORDER = (
    "contact_delivery",
    "contact_grant",
    "contact_request",
    "contact_access_audit",
    "recommendation_impression",
    "recommendation_exposure_daily",
    "wecom_outbound_outbox",
    "recommendation_delivery",
    "recommendation_search_attempt",
    "recommendation_request",
    "action_execution",
    "action_parse_artifact",
    "domain_outbox_event",
    "wecom_inbound_event",
    "conversation_log",
    "event_log",
    "resume_media_isolation_issue",
    "media_asset_lifecycle",
    "target_cleanup_task",
    "job_replacement",
    "resume_replacement",
    "audit_log",
    "job",
    "resume",
    "user",
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def ensure_control_plane_enabled() -> None:
    """Fail closed before even read-only demo administration."""
    if not settings.demo_mode_enabled:
        raise DemoAdminError("demo mode is disabled")
    if settings.app_env.lower().strip() not in {"development", "test"}:
        raise DemoAdminError("demo mode is unavailable outside development/test")


def _workspace(db: Session, demo_id: str) -> DemoWorkspace:
    ensure_control_plane_enabled()
    value = str(demo_id or "").strip()
    row = db.query(DemoWorkspace).filter(DemoWorkspace.demo_id == value).first()
    if row is None:
        raise DemoAdminNotFound("demo workspace not found")
    if row.status not in _ACTIVE_STATUSES:
        raise DemoAdminConflict("demo workspace has an invalid status")
    return row


def _table_exists(db: Session, model: Any) -> bool:
    try:
        return bool(inspect(db.get_bind()).has_table(model.__tablename__))
    except Exception:
        return False


def _principal_userids(db: Session, demo_id: str) -> list[str]:
    return [
        str(value)
        for value, in db.query(DemoPrincipal.synthetic_userid).filter(
            DemoPrincipal.demo_id == demo_id,
            DemoPrincipal.principal_status == "active",
        ).all()
    ]


def _resource_rows(db: Session, demo_id: str) -> list[DemoResource]:
    return db.query(DemoResource).filter(DemoResource.demo_id == demo_id).all()


def _scoped_targets(db: Session, demo_id: str) -> dict[str, set[str]]:
    """Build an explicit, deterministic target set for the workspace."""
    result: dict[str, set[str]] = defaultdict(set)
    for row in _resource_rows(db, demo_id):
        result[row.resource_type].add(str(row.target_id))

    userids = _principal_userids(db, demo_id)
    if userids:
        result["user"].update(userids)
        for model, column_name in _USER_SCOPED_MODELS:
            if not _table_exists(db, model):
                continue
            column = getattr(model, column_name, None)
            if column is None:
                continue
            # The owner/actor column is only the scope predicate.  Cleanup
            # needs the actual primary key (e.g. request_id for
            # RecommendationRequest, parse_ref for ActionParseArtifact), not
            # the userid itself.  ``id`` is not universal across these tables.
            mapper = inspect(model)
            primary_key = mapper.primary_key[0].key if mapper.primary_key else column_name
            key_column = getattr(model, primary_key, column)
            rows = db.query(key_column).filter(column.in_(userids)).all()
            result[model.__tablename__].update(
                str(getattr(row, primary_key)) for row in rows
            )

    # Search attempts have no viewer/owner column, so synthetic-principal
    # ownership alone cannot discover them.  Collect both the explicit
    # workspace discriminator and attempts linked to the scoped request ids;
    # the request relation also covers rows written before demo_id stamping
    # was introduced and keeps cleanup retryable for mixed-version data.
    if _table_exists(db, RecommendationSearchAttempt):
        request_ids = result.get("recommendation_request", set())
        attempt_query = db.query(RecommendationSearchAttempt.attempt_id).filter(
            or_(
                RecommendationSearchAttempt.demo_id == demo_id,
                RecommendationSearchAttempt.request_id.in_(list(request_ids))
                if request_ids else RecommendationSearchAttempt.request_id.is_(None),
            ),
        )
        result["recommendation_search_attempt"].update(
            str(value) for (value,) in attempt_query.all()
        )
    if _table_exists(db, RecommendationExposureDaily):
        if db.query(RecommendationExposureDaily.demo_id).filter(
            RecommendationExposureDaily.demo_id == demo_id,
        ).first() is not None:
            result["recommendation_exposure_daily"].add(demo_id)
    return result


def _count_targets(db: Session, targets: dict[str, set[str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for resource_type, ids in targets.items():
        if resource_type == "user":
            model, key_name = User, "external_userid"
        elif resource_type not in _EXACT_RESOURCE_MODELS:
            counts[resource_type] = len(ids)
            continue
        else:
            model, key_name = _EXACT_RESOURCE_MODELS[resource_type]
        if not ids or not _table_exists(db, model):
            counts[resource_type] = 0
            continue
        column = getattr(model, key_name)
        counts[resource_type] = int(
            db.query(model).filter(column.in_(list(ids))).count()
        )
    return dict(sorted(counts.items()))


def workspace_status(db: Session, *, demo_id: str) -> dict[str, Any]:
    row = _workspace(db, demo_id)
    resources = _resource_rows(db, row.demo_id)
    principals = db.query(DemoPrincipal).filter(DemoPrincipal.demo_id == row.demo_id).all()
    members = db.query(DemoWorkspaceMember).filter(DemoWorkspaceMember.demo_id == row.demo_id).all()
    targets = _scoped_targets(db, row.demo_id)
    return {
        "demo_id": row.demo_id,
        "name": row.name,
        "status": row.status,
        "bot_id": row.bot_id,
        "actor_digest": row.opaque_actor_digest,
        "created_by": row.created_by,
        "reason": row.reason,
        "version": int(row.version or 1),
        "created_at": row.created_at,
        "disabled_at": row.disabled_at,
        "cleaned_at": row.cleaned_at,
        "principals": [
            {"role": p.role, "synthetic_userid": p.synthetic_userid, "status": p.principal_status}
            for p in principals
        ],
        "members": [
            {"member_id": m.member_id, "actor_digest": m.opaque_actor_digest,
             "status": m.membership_status, "expires_at": m.expires_at}
            for m in members
        ],
        "resource_counts": dict(Counter(r.resource_type for r in resources)),
        "scoped_target_counts": _count_targets(db, targets),
        "resource_status_counts": dict(Counter(r.lifecycle_status for r in resources)),
    }


def preview_workspace(db: Session, *, demo_id: str) -> dict[str, Any]:
    row = _workspace(db, demo_id)
    targets = _scoped_targets(db, row.demo_id)
    return {
        "demo_id": row.demo_id,
        "status": row.status,
        "version": int(row.version or 1),
        "would_disable": row.status == "active",
        "scope_policy": "demo_resource + exact synthetic principal ownership",
        "counts": _count_targets(db, targets),
        "total_rows": sum(_count_targets(db, targets).values()),
        "resource_types": sorted(targets),
        "no_prefix_scan": True,
        "no_real_actor_user_cleanup": True,
    }


def list_workspaces(db: Session, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    ensure_control_plane_enabled()
    query = db.query(DemoWorkspace)
    if status:
        query = query.filter(DemoWorkspace.status == status.strip())
    rows = query.order_by(DemoWorkspace.created_at.desc()).limit(max(1, min(limit, 200))).all()
    return [workspace_status(db, demo_id=row.demo_id) for row in rows]


def _check_version(row: DemoWorkspace, expected_version: int | None) -> None:
    if expected_version is not None and int(row.version or 1) != int(expected_version):
        raise DemoAdminConflict("demo workspace version conflict")


def disable_workspace(
    db: Session, *, demo_id: str, reason: str, operator: str,
    expected_version: int | None = None,
) -> dict[str, Any]:
    row = _workspace(db, demo_id)
    _check_version(row, expected_version)
    if row.status in {"cleaned", "cleaning", "failed"}:
        raise DemoAdminConflict("workspace cannot be disabled in its current state")
    if row.status == "disabled":
        return workspace_status(db, demo_id=row.demo_id)

    before = {"status": row.status, "version": int(row.version or 1)}
    now = _now()
    row.status = "disabled"
    row.reason = reason.strip()[:255]
    row.disabled_at = now
    row.version = int(row.version or 1) + 1
    for member in db.query(DemoWorkspaceMember).filter(
        DemoWorkspaceMember.demo_id == row.demo_id,
        DemoWorkspaceMember.membership_status == "active",
    ).all():
        member.membership_status = "revoked"
        member.revoked_at = now
    for resource in _resource_rows(db, row.demo_id):
        if resource.lifecycle_status == "active":
            resource.lifecycle_status = "delisted"
    _soft_delist_fact_sources(db, row.demo_id, now)
    write_admin_log(
        db, target_type="system", target_id=row.demo_id, action="manual_edit",
        operator=operator, before=before,
        after={"status": row.status, "version": int(row.version)},
        reason=f"demo_disable:{reason.strip()[:220]}",
    )
    db.commit()
    return workspace_status(db, demo_id=row.demo_id)


def _soft_delist_fact_sources(db: Session, demo_id: str, now: datetime) -> None:
    userids = _principal_userids(db, demo_id)
    if not userids:
        return
    for model in (Job, Resume):
        if not _table_exists(db, model):
            continue
        rows = db.query(model).filter(model.owner_userid.in_(userids)).all()
        for item in rows:
            item.deleted_at = item.deleted_at or now
            if hasattr(item, "delist_reason"):
                item.delist_reason = "manual_delist"


def _delete_exact(db: Session, resource_type: str, ids: set[str]) -> int:
    if not ids:
        return 0
    if resource_type == "user":
        model, key_name = User, "external_userid"
    elif resource_type in _EXACT_RESOURCE_MODELS:
        model, key_name = _EXACT_RESOURCE_MODELS[resource_type]
    else:
        return 0
    if resource_type == "user":
        # Use a table-level DELETE for synthetic users. ORM bulk-delete
        # synchronization can inspect stale User instances kept in the
        # session from workspace creation and, on SQLite, issue the DELETE
        # before the just-flushed principal removal is visible to its FK
        # bookkeeping. The table-level statement is still parameterized and
        # exact-id scoped, while avoiding ORM identity-map side effects.
        # Use DBAPI-bound placeholders instead of ORM/Core compilation here.
        # SQLite's FK checker can otherwise retain a stale mapped-table
        # dependency during this same-transaction checkpoint delete.
        placeholders = ", ".join("?" for _ in ids)
        result = db.connection().exec_driver_sql(
            f"DELETE FROM `{model.__tablename__}` "
            f"WHERE `{key_name}` IN ({placeholders})",
            tuple(ids),
        )
        return int(result.rowcount or 0)
    if not _table_exists(db, model):
        return 0
    column = getattr(model, key_name)
    return int(db.query(model).filter(column.in_(list(ids))).delete(synchronize_session=False))


def _delete_user_scoped(db: Session, userids: set[str]) -> dict[str, int]:
    deleted: dict[str, int] = {}
    for model, column_name in _USER_SCOPED_MODELS:
        # Fact sources are deliberately handled at their explicit lifecycle
        # stages below. Deleting them here would violate the documented FK /
        # dependency order and make a partial retry less predictable.
        if model in {Job, Resume}:
            continue
        if not userids or not _table_exists(db, model):
            continue
        column = getattr(model, column_name, None)
        if column is None:
            continue
        count = int(db.query(model).filter(column.in_(list(userids))).delete(synchronize_session=False))
        if count:
            deleted[model.__tablename__] = count
    return deleted


def _clear_demo_redis(demo_id: str, actor_userid: str | None) -> int:
    """Delete only namespaced demo keys; never scan real session keys."""
    client = get_redis()
    keys: set[str] = set()
    for pattern in (f"demo:session:{demo_id}:*",):
        keys.update(str(key) for key in client.scan_iter(match=pattern))
    if actor_userid:
        keys.add(f"demo:active:{actor_userid}")
    if keys:
        client.delete(*list(keys))
    return len(keys)


def cleanup_workspace(
    db: Session, *, demo_id: str, reason: str, operator: str,
    expected_version: int | None = None,
) -> dict[str, Any]:
    row = _workspace(db, demo_id)
    _check_version(row, expected_version)
    if row.status == "cleaned":
        return workspace_status(db, demo_id=row.demo_id)
    if row.status == "active":
        disable_workspace(db, demo_id=row.demo_id, reason=reason, operator=operator)
        row = _workspace(db, demo_id)

    # Transition and commit first. A process crash after this point leaves a
    # resumable ``cleaning`` checkpoint instead of re-opening the workspace.
    row.status = "cleaning"
    row.reason = reason.strip()[:255]
    row.version = int(row.version or 1) + 1
    db.commit()

    before = {"status": "cleaning", "version": int(row.version)}
    try:
        targets = _scoped_targets(db, row.demo_id)
        deleted: dict[str, int] = {}
        userids = set(_principal_userids(db, row.demo_id))
        member_actor_userids = {
            str(value)
            for value, in db.query(DemoWorkspaceMember.canonical_actor_userid).filter(
                DemoWorkspaceMember.demo_id == row.demo_id,
                DemoWorkspaceMember.canonical_actor_userid.isnot(None),
            ).all()
        }
        # Remove active pointers before deleting principals. If Redis is
        # unavailable we fail before touching business rows, so retry is safe.
        redis_deleted = _clear_demo_redis(row.demo_id, row.canonical_actor_userid)
        for actor_userid in member_actor_userids:
            if actor_userid != row.canonical_actor_userid:
                redis_deleted += _clear_demo_redis(row.demo_id, actor_userid)
        for resource_type in _CLEANUP_ORDER:
            if resource_type == "user":
                # ``demo_principal.synthetic_userid`` is an explicit RESTRICT
                # FK to user.external_userid.  Keep principal deletion and
                # synthetic-user deletion in one transaction: if either side
                # fails, the rollback preserves the principal rows so a retry
                # can reconstruct the exact same cleanup scope.  Committing
                # the principal delete first would make a later failure leave
                # orphaned synthetic users with no durable owner list.
                user_scoped_deleted = _delete_user_scoped(db, userids)
                # Delete through the same connection as the subsequent User
                # delete. This keeps the FK transition deterministic even
                # when this Session still has principal objects in its
                # identity map from a previous status read.
                for identity_obj in tuple(db.identity_map.values()):
                    if isinstance(identity_obj, (DemoPrincipal, User)):
                        db.expunge(identity_obj)
                db.connection().execute(
                    DemoPrincipal.__table__.delete().where(
                        DemoPrincipal.__table__.c.demo_id == row.demo_id,
                    )
                )
                # Force the FK-visible delete to reach the database before
                # deleting synthetic users. This is still one transaction;
                # the explicit flush makes the ordering deterministic across
                # SQLite test databases and MySQL production databases.
                db.flush()
                deleted.update(user_scoped_deleted)
                deleted["user"] = _delete_exact(db, "user", targets.get("user", set()))
            elif resource_type in {"job", "resume"}:
                deleted[resource_type] = _delete_exact(db, resource_type, targets.get(resource_type, set()))
            else:
                deleted[resource_type] = _delete_exact(db, resource_type, targets.get(resource_type, set()))
            db.commit()  # every cleanup stage is independently durable

        # The registry remains as a cleaned audit trail. Principal rows were
        # deleted before synthetic users in the ordered cleanup stage above.
        db.query(DemoWorkspaceMember).filter(DemoWorkspaceMember.demo_id == row.demo_id).update(
            {DemoWorkspaceMember.membership_status: "revoked", DemoWorkspaceMember.revoked_at: _now()},
            synchronize_session=False,
        )
        for resource in _resource_rows(db, row.demo_id):
            resource.lifecycle_status = "cleaned"
            resource.cleaned_at = _now()
            resource.last_error = None
        row.status = "cleaned"
        row.cleaned_at = _now()
        row.version = int(row.version or 1) + 1
        db.commit()
        write_admin_log(
            db, target_type="system", target_id=row.demo_id, action="manual_edit",
            operator=operator, before=before,
            after={"status": row.status, "version": int(row.version), "deleted": deleted},
            reason=f"demo_cleanup:{reason.strip()[:220]}",
        )
        db.commit()
        result = workspace_status(db, demo_id=row.demo_id)
        result.update({"deleted_counts": deleted, "redis_keys_deleted": redis_deleted})
        return result
    except Exception as exc:
        db.rollback()
        failed = db.query(DemoWorkspace).filter(DemoWorkspace.demo_id == demo_id).first()
        if failed is not None:
            failed.status = "failed"
            failed.reason = f"{reason.strip()[:180]} [error: {str(exc)[:60]}]"
            failed.version = int(failed.version or 1) + 1
            db.commit()
        raise DemoAdminError(f"demo cleanup failed: {str(exc)[:180]}") from exc


def retry_cleanup(
    db: Session, *, demo_id: str, reason: str, operator: str,
    expected_version: int | None = None,
) -> dict[str, Any]:
    row = _workspace(db, demo_id)
    _check_version(row, expected_version)
    if row.status not in {"failed", "cleaning", "disabled"}:
        if row.status == "cleaned":
            return workspace_status(db, demo_id=row.demo_id)
        raise DemoAdminConflict("workspace is not retryable")
    return cleanup_workspace(
        db, demo_id=row.demo_id, reason=reason, operator=operator,
        expected_version=int(row.version or 1),
    )
