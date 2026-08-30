"""Public Resume listing profile and field safety contract."""
from __future__ import annotations

from dataclasses import dataclass

PROFILE = "recruitment.resume"
PROFILE_VERSION = "resume-profile-v1"
HARD_FIELDS = ("expected_cities", "expected_job_categories", "salary_expect_floor_monthly", "gender", "age", "accept_long_term", "accept_short_term")
SOFT_FIELDS = ("expected_districts", "education", "work_experience", "accept_night_shift", "accept_standing_work", "accept_overtime", "accept_outside_province", "couple_seeking_together", "has_health_certificate", "available_from", "has_tattoo", "taboo")
SENSITIVE_FIELDS = frozenset({"age", "gender", "phone", "wechat", "contact_person", "health_certificate", "taboo", "raw_text"})


@dataclass(frozen=True)
class ResumeProfile:
    name: str = PROFILE
    version: str = PROFILE_VERSION
    hard_fields: tuple[str, ...] = HARD_FIELDS
    soft_fields: tuple[str, ...] = SOFT_FIELDS
    sensitive_fields: frozenset[str] = SENSITIVE_FIELDS

    def missing_hard_fields(self, values: dict) -> tuple[str, ...]:
        missing = []
        for field in self.hard_fields:
            value = values.get(field)
            if value is None or value == "" or value == []:
                missing.append(field)
        if values.get("accept_long_term") is False and values.get("accept_short_term") is False:
            missing.append("accept_long_term_or_short_term")
        return tuple(missing)

    def redact(self, values: dict, *, include_sensitive: bool = False) -> dict:
        if include_sensitive:
            return dict(values)
        return {key: value for key, value in values.items() if key not in self.sensitive_fields}


RESUME_PROFILE = ResumeProfile()

