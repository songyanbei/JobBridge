"""Versioned, privacy-safe contract for the S4 job publish flow."""
from __future__ import annotations

from typing import Any

PROFILE_NAME = "job_publish_v1"
PROFILE_VERSION = "job_publish_v1"

# Fields accepted from intent extraction. Contact values are accepted for the
# draft owner only and are never exposed through the listing profile.
JOB_HARD_FIELDS = frozenset({
    "hiring_company", "city", "district", "address", "job_category",
    "headcount", "salary_floor_monthly", "salary_ceiling_monthly", "pay_type",
    "gender_required", "age_min", "age_max", "is_long_term",
})
JOB_SOFT_FIELDS = frozenset({
    "provide_meal", "provide_housing", "dorm_condition", "shift_pattern",
    "work_hours", "accept_couple", "accept_student", "accept_minority",
    "height_required", "experience_required", "education_required", "rebate",
    "employment_type", "contract_type", "min_duration", "job_sub_category",
    "description", "raw_text", "images", "miniprogram_url", "contact_person", "phone",
})
JOB_ALLOWED_FIELDS = JOB_HARD_FIELDS | JOB_SOFT_FIELDS
JOB_REQUIRED_FIELDS = frozenset({"city", "job_category", "salary_floor_monthly", "pay_type", "headcount"})
JOB_ACTIONS = frozenset({"publish_job", "edit_job_draft", "confirm_job", "delist_job", "restore_job"})


def normalize_job_fields(values: dict[str, Any] | None) -> dict[str, Any]:
    """Drop unknown fields and normalize scalar whitespace without mutating input."""
    values = values or {}
    result: dict[str, Any] = {}
    for key, value in values.items():
        key = str(key)
        if key not in JOB_ALLOWED_FIELDS or value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        result[key] = value
    if "is_long_term" in result:
        result["is_long_term"] = bool(result["is_long_term"])
    return result


def missing_job_fields(values: dict[str, Any] | None) -> list[str]:
    normalized = normalize_job_fields(values)
    return sorted(field for field in JOB_REQUIRED_FIELDS if field not in normalized)


def job_profile_contract() -> dict[str, Any]:
    """Return a serializable contract for diagnostics and preflight checks."""
    return {
        "profile": PROFILE_NAME,
        "version": PROFILE_VERSION,
        "actions": sorted(JOB_ACTIONS),
        "required_fields": sorted(JOB_REQUIRED_FIELDS),
        "allowed_fields": sorted(JOB_ALLOWED_FIELDS),
    }


__all__ = [
    "JOB_ACTIONS", "JOB_ALLOWED_FIELDS", "JOB_HARD_FIELDS", "JOB_REQUIRED_FIELDS",
    "JOB_SOFT_FIELDS", "PROFILE_NAME", "PROFILE_VERSION", "job_profile_contract",
    "missing_job_fields", "normalize_job_fields",
]
