"""Deterministic recommendation match reasons.

Reason builders only accept the safe projection DTOs in this module. They must
not receive raw ORM objects or arbitrary result dictionaries because those may
contain contact details or audit-sensitive text.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from app.schemas.search import MatchReason


@dataclass(frozen=True)
class JobExplanationInput:
    id: str
    company_name: str = ""
    category: str = ""
    city: str = ""
    district: str = ""
    salary_min: int | None = None
    salary_max: int | None = None
    shift: str = ""
    benefits: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ResumeExplanationInput:
    id: str
    name: str = ""
    expected_city: tuple[str, ...] = field(default_factory=tuple)
    expected_category: tuple[str, ...] = field(default_factory=tuple)
    expected_salary_min: int | None = None
    skills: tuple[str, ...] = field(default_factory=tuple)
    availability: str = ""
    gender: str = ""
    age: int | None = None


def project_job_for_explanation(filtered_job: dict[str, Any]) -> JobExplanationInput:
    benefits: list[str] = []
    if filtered_job.get("provide_meal"):
        benefits.append("包吃")
    if filtered_job.get("provide_housing"):
        benefits.append("包住")
    return JobExplanationInput(
        id=str(filtered_job.get("id", "")),
        company_name=str(filtered_job.get("company") or filtered_job.get("company_name") or ""),
        category=str(filtered_job.get("job_category") or filtered_job.get("category") or ""),
        city=str(filtered_job.get("city") or ""),
        district=str(filtered_job.get("district") or ""),
        salary_min=_maybe_int(filtered_job.get("salary_floor_monthly")),
        salary_max=_maybe_int(filtered_job.get("salary_ceiling_monthly")),
        shift=str(filtered_job.get("shift_pattern") or filtered_job.get("shift") or ""),
        benefits=tuple(benefits),
    )


def project_resume_for_explanation(filtered_resume: dict[str, Any]) -> ResumeExplanationInput:
    return ResumeExplanationInput(
        id=str(filtered_resume.get("id", "")),
        name=str(filtered_resume.get("display_name") or filtered_resume.get("name") or "求职者"),
        expected_city=_as_tuple(filtered_resume.get("expected_cities")),
        expected_category=_as_tuple(filtered_resume.get("expected_job_categories")),
        expected_salary_min=_maybe_int(filtered_resume.get("salary_expect_floor_monthly")),
        skills=_as_tuple(filtered_resume.get("skills")),
        availability=str(filtered_resume.get("availability") or ""),
        gender=str(filtered_resume.get("gender") or ""),
        age=_maybe_int(filtered_resume.get("age")),
    )


def build_match_reasons(
    *,
    item: JobExplanationInput | ResumeExplanationInput,
    criteria: dict[str, Any],
    item_type: Literal["job", "resume"],
    soft_pref_hits: dict[str, Any] | None = None,
    include_soft_preferences: bool = False,
    limit: int = 2,
) -> list[MatchReason]:
    """Build at most ``limit`` safe, deterministic match reasons."""
    reasons: list[MatchReason] = []
    if item_type == "job":
        reasons.extend(_job_hard_reasons(item, criteria))  # type: ignore[arg-type]
    else:
        reasons.extend(_resume_hard_reasons(item, criteria))  # type: ignore[arg-type]

    if include_soft_preferences and soft_pref_hits:
        reasons.extend(_soft_preference_reasons(soft_pref_hits))
    return reasons[:limit]


def render_match_reasons(reasons: list[MatchReason]) -> list[str]:
    return [f"   匹配依据：{reason.text}" for reason in reasons]


def _job_hard_reasons(item: JobExplanationInput, criteria: dict[str, Any]) -> list[MatchReason]:
    reasons: list[MatchReason] = []
    cities = set(_as_tuple(criteria.get("city")))
    cats = set(_as_tuple(criteria.get("job_category")))
    salary_floor = _maybe_int(criteria.get("salary_floor_monthly"))
    if item.city and (not cities or item.city in cities):
        location = f"{item.city}{item.district}" if item.district else item.city
        reasons.append(MatchReason("hard_match", f"地点符合 {location}", "city"))
    if item.category and (not cats or item.category in cats):
        reasons.append(MatchReason("hard_match", f"工种匹配 {item.category}", "job_category"))
    if salary_floor is not None and item.salary_min is not None:
        salary_max = item.salary_max if item.salary_max is not None else item.salary_min
        if salary_max >= salary_floor:
            reasons.append(MatchReason("hard_match", f"薪资覆盖你的 {salary_floor} 元/月要求", "salary_floor_monthly"))
    return reasons


def _resume_hard_reasons(item: ResumeExplanationInput, criteria: dict[str, Any]) -> list[MatchReason]:
    reasons: list[MatchReason] = []
    cities = set(_as_tuple(criteria.get("city")))
    cats = set(_as_tuple(criteria.get("job_category")))
    salary_ceiling = _maybe_int(criteria.get("salary_ceiling_monthly"))
    if item.expected_city and (not cities or cities.intersection(item.expected_city)):
        reasons.append(MatchReason("hard_match", f"期望城市包含 {'、'.join(item.expected_city[:2])}", "city"))
    if item.expected_category and (not cats or cats.intersection(item.expected_category)):
        reasons.append(MatchReason("hard_match", f"期望工种匹配 {'/'.join(item.expected_category[:2])}", "job_category"))
    if salary_ceiling is not None and item.expected_salary_min is not None:
        if item.expected_salary_min <= salary_ceiling:
            reasons.append(MatchReason("hard_match", f"期望薪资不高于 {salary_ceiling} 元/月", "salary_ceiling_monthly"))
    return reasons


def _soft_preference_reasons(soft_pref_hits: dict[str, Any]) -> list[MatchReason]:
    labels = {
        "provide_meal": "包吃",
        "provide_housing": "包住",
        "shift_pattern": "班次",
        "is_long_term": "长期",
    }
    reasons: list[MatchReason] = []
    for field, hit in soft_pref_hits.items():
        if hit:
            label = labels.get(field, field)
            reasons.append(MatchReason("soft_preference", f"匹配你提到的{label}偏好", field))
    return reasons


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set)):
        return tuple(str(v) for v in value if v not in (None, ""))
    if value == "":
        return ()
    return (str(value),)


def _maybe_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None
