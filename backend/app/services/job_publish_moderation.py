"""Deterministic moderation policy for job publish candidates."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.listing.job_profile import missing_job_fields, normalize_job_fields
from app.models import AuditLog, Job

MODERATION_RULE_VERSION = "job_moderation_v1"
_PAY_TYPES = {"月薪", "时薪", "计件"}


@dataclass(frozen=True)
class JobModerationDecision:
    status: str  # passed / pending / rejected
    reason: str = ""
    rule_version: str = MODERATION_RULE_VERSION
    matched_rules: tuple[str, ...] = field(default_factory=tuple)


def moderate_job_fields(
    fields: dict[str, Any],
    *,
    raw_text: str = "",
    db=None,
    rule_version: str = MODERATION_RULE_VERSION,
) -> JobModerationDecision:
    """Evaluate only allowlisted fields with stable, explainable rules."""
    values = normalize_job_fields(fields)
    missing = missing_job_fields(values)
    if missing:
        return JobModerationDecision("pending", "missing_fields:" + ",".join(missing), rule_version, ("required_fields",))
    matched: list[str] = []
    reasons: list[str] = []
    salary_floor = values.get("salary_floor_monthly")
    if not isinstance(salary_floor, int) or salary_floor <= 0 or salary_floor > 1_000_000:
        matched.append("salary_range")
        reasons.append("invalid_salary")
    ceiling = values.get("salary_ceiling_monthly")
    if ceiling is not None and (not isinstance(ceiling, int) or ceiling < salary_floor):
        matched.append("salary_order")
        reasons.append("salary_ceiling_below_floor")
    headcount = values.get("headcount")
    if not isinstance(headcount, int) or not 1 <= headcount <= 10000:
        matched.append("headcount_range")
        reasons.append("invalid_headcount")
    if values.get("pay_type") not in _PAY_TYPES:
        matched.append("pay_type")
        reasons.append("invalid_pay_type")
    age_min, age_max = values.get("age_min"), values.get("age_max")
    if age_min is not None and age_max is not None and age_min > age_max:
        matched.append("age_order")
        reasons.append("age_range_invalid")
    if db is not None:
        from app.services.audit_service import audit_content_only
        audit = audit_content_only(raw_text or str(values), db)
        if audit.status == "rejected":
            matched.append("sensitive_content")
            reasons.append(audit.reason or "sensitive_content")
        elif audit.status == "pending":
            matched.append("sensitive_review")
            reasons.append(audit.reason or "manual_review")
    if "sensitive_content" in matched or any(rule in matched for rule in ("salary_range", "salary_order", "headcount_range", "pay_type", "age_order")):
        return JobModerationDecision("rejected", ";".join(reasons), rule_version, tuple(matched))
    if "sensitive_review" in matched:
        return JobModerationDecision("pending", ";".join(reasons), rule_version, tuple(matched))
    return JobModerationDecision("passed", ";".join(reasons), rule_version, tuple(matched))


def apply_job_moderation(
    db,
    job_id: int,
    decision: JobModerationDecision,
    *,
    operator: str = "system",
    expected_version: int | None = None,
) -> Job:
    """Apply a decision under a row lock; stale writes fail closed."""
    job = db.query(Job).filter(Job.id == job_id).with_for_update().first()
    if job is None:
        raise ValueError("job_not_found")
    current_version = int(job.version or 0)
    if expected_version is not None and current_version != int(expected_version):
        raise ValueError("stale_job_version")
    if job.audit_status == decision.status and job.audit_reason == (decision.reason or None):
        return job
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    job.audit_status = decision.status
    job.audit_reason = decision.reason or None
    job.audited_by = operator
    job.audited_at = now
    job.version = current_version + 1
    db.add(AuditLog(
        target_type="job", target_id=str(job.id), action=f"moderation_{decision.status}",
        reason=f"{decision.rule_version}:{decision.reason}" if decision.reason else decision.rule_version,
        operator=operator,
    ))
    db.flush()
    return job


__all__ = ["JobModerationDecision", "MODERATION_RULE_VERSION", "apply_job_moderation", "moderate_job_fields"]
