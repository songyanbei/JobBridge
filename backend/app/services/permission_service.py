"""Final structured-candidate field filtering for recommendation visibility."""
from __future__ import annotations

from typing import Mapping

from app.services.visibility_contract import (
    ViewerRole,
    VisibilityScene,
    hard_visibility_limit,
    registry_for,
)
from app.services.visibility_policy import EffectivePolicySnapshot


PUBLISHER_ROLES = frozenset({"factory", "broker"})


def can_publish_job(user_or_context, *, owner_userid: str | None = None) -> bool:
    """Deterministic S4 publisher matrix.

    Workers are never allowed to publish/manage jobs.  Factories may publish
    their own jobs; brokers must be active and have at least one recruiting
    capability flag enabled.  Unknown/missing attributes fail closed so this
    helper works with both ORM ``User`` rows and ``UserContext`` adapters.
    """
    role = getattr(user_or_context, "role", None)
    userid = getattr(user_or_context, "external_userid", None)
    status = getattr(user_or_context, "status", None)
    if role not in PUBLISHER_ROLES or status in {"blocked", "deleted", "disabled"}:
        return False
    if owner_userid is not None and userid != owner_userid:
        return False
    if role == "factory":
        return True
    return bool(
        getattr(user_or_context, "can_search_workers", False)
        or getattr(user_or_context, "can_search_jobs", False)
        or getattr(user_or_context, "broker_recruiting_enabled", False)
    )


def can_manage_job(user_or_context, job) -> bool:
    """Publisher ownership guard used by message and admin compatibility paths."""
    return can_publish_job(user_or_context, owner_userid=getattr(job, "owner_userid", None))

_JOB_CANDIDATE_KEYS: Mapping[str, tuple[str, ...]] = {
    "hiring_company": ("hiring_company", "hiring_company_source"),
    "job_category": ("job_category",),
    "salary": ("salary_floor_monthly", "salary_ceiling_monthly", "pay_type"),
    "city": ("city",),
    "district": ("district",),
    "address": ("address", "address_source"),
    "benefits": ("provide_meal", "provide_housing"),
    "shift": ("shift_pattern", "work_hours"),
    # Sensitive contact values are never projected into search cards.  The
    # policy fields remain as compatibility identifiers, but map to the safe
    # availability/placeholder representation.
    "contact_person": ("contact_available", "contact_placeholder"),
    "phone": ("contact_available", "contact_placeholder", "phone_placeholder"),
    "publisher_company": ("publisher_company",),
}

_RESUME_CANDIDATE_KEYS: Mapping[str, tuple[str, ...]] = {
    "display_name": ("display_name",),
    "gender_age": ("gender", "age"),
    "expected_job_categories": ("expected_job_categories",),
    "salary_expectation": ("salary_expect_floor_monthly",),
    "expected_cities": ("expected_cities",),
    "phone": ("contact_available", "contact_placeholder", "phone_placeholder"),
}


def _snapshot_matches(
    snapshot: EffectivePolicySnapshot,
    scene: VisibilityScene,
    role: str,
) -> bool:
    return snapshot.scene is scene and snapshot.role == role


def _project_visible_candidate(
    candidate: Mapping,
    visible_fields: tuple[str, ...],
    key_map: Mapping[str, tuple[str, ...]],
) -> dict:
    result = {"id": candidate.get("id")}
    for field in visible_fields:
        for key in key_map.get(field, ()):
            if key in candidate:
                result[key] = candidate[key]
    return result


def _effective_visible_fields(
    snapshot: EffectivePolicySnapshot,
    scene: VisibilityScene,
    role: ViewerRole,
) -> tuple[str, ...]:
    hard_limit = set(hard_visibility_limit(scene, role))
    return tuple(
        field for field in registry_for(scene)
        if field in snapshot.visible_fields and field in hard_limit
    )


def filter_job_for_role(
    job_data: dict,
    viewer_role: str,
    effective_policy: EffectivePolicySnapshot | None,
) -> dict:
    """Filter a normalized job using policy whitelist ∩ backend hard limit."""

    try:
        role = ViewerRole(viewer_role)
    except ValueError:
        return {"id": job_data.get("id")}

    if effective_policy is None:
        return {"id": job_data.get("id")}
    if not _snapshot_matches(effective_policy, VisibilityScene.JOB_SEARCH, viewer_role):
        return {"id": job_data.get("id")}
    return _project_visible_candidate(
        job_data,
        _effective_visible_fields(
            effective_policy, VisibilityScene.JOB_SEARCH, role,
        ),
        _JOB_CANDIDATE_KEYS,
    )


def filter_resume_for_role(
    resume_data: dict,
    owner_user: dict | None,
    viewer_role: str,
    effective_policy: EffectivePolicySnapshot | None,
) -> dict:
    """Filter a normalized resume and its owner data through one policy snapshot."""

    try:
        role = ViewerRole(viewer_role)
    except ValueError:
        return {"id": resume_data.get("id")}

    candidate = dict(resume_data)
    if owner_user:
        candidate["display_name"] = owner_user.get("display_name") or "求职者"
        candidate["contact_available"] = bool(owner_user.get("contact_available"))
    else:
        candidate["display_name"] = "求职者"
        candidate["contact_available"] = False

    if effective_policy is None:
        return {"id": resume_data.get("id")}
    if not _snapshot_matches(
        effective_policy, VisibilityScene.CANDIDATE_SEARCH, viewer_role,
    ):
        return {"id": resume_data.get("id")}
    visible_fields = _effective_visible_fields(
        effective_policy, VisibilityScene.CANDIDATE_SEARCH, role,
    )
    # Keep the placeholder scoped to the sensitive field that policy exposed;
    # never add a contact marker when phone is hidden by policy.
    if "phone" in visible_fields:
        # Preserve the legacy non-PII wording consumed by admin/search
        # contracts; the field remains an opaque contact-request prompt.
        candidate["contact_placeholder"] = "联系方式不完整，请通过联系申请获取"
        candidate["phone_placeholder"] = "联系方式待补充"
    return _project_visible_candidate(
        candidate, visible_fields, _RESUME_CANDIDATE_KEYS,
    )


def filter_jobs_batch(
    jobs: list[dict],
    viewer_role: str,
    effective_policy: EffectivePolicySnapshot | None,
) -> list[dict]:
    return [
        filter_job_for_role(job, viewer_role, effective_policy) for job in jobs
    ]


def filter_resumes_batch(
    resumes: list[dict],
    users_map: dict[str, dict],
    viewer_role: str,
    effective_policy: EffectivePolicySnapshot | None,
) -> list[dict]:
    return [
        filter_resume_for_role(
            resume,
            users_map.get(resume.get("owner_userid", "")),
            viewer_role,
            effective_policy,
        )
        for resume in resumes
    ]
