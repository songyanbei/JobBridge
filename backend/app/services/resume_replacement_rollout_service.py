"""Strict parser for the hidden database-backed replacement allowlist."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re

from sqlalchemy.orm import Session

from app.core.exceptions import BusinessException
from app.models import SystemConfig
from app.services.admin_log_service import write_admin_log

ROLLOUT_CONFIG_KEY = "resume.replacement.rollout.allowlist"
# A short-lived pre-freeze build wrote this spelling.  Migration validation
# must fail closed when it is present, even though new writes use only the
# canonical public-contract key above.
LEGACY_ROLLOUT_CONFIG_KEYS = ("rollout.resume_replacement.allowlist",)
MAX_ALLOWLIST_REVISION = 18_446_744_073_709_551_615
_SENSITIVE_REASON = re.compile(
    r"(?:https?://|\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b|(?<!\d)1\d{10}(?!\d)|[/\\])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ResumeReplacementAllowlist:
    revision: int
    userids: tuple[str, ...]


def validate_allowlist(payload: object) -> ResumeReplacementAllowlist:
    if not isinstance(payload, dict) or set(payload) != {"revision", "userids"}:
        raise ValueError("allowlist schema invalid")
    revision = payload["revision"]
    members = payload["userids"]
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or not 1 <= revision <= MAX_ALLOWLIST_REVISION
    ):
        raise ValueError("allowlist revision must be a positive integer")
    if not isinstance(members, list) or any(
        not isinstance(item, str)
        or not item.strip()
        or item != item.strip()
        or len(item) > 64
        for item in members
    ):
        raise ValueError("allowlist userids must be non-empty strings")
    if len(set(members)) != len(members):
        raise ValueError("allowlist userids must be unique")
    return ResumeReplacementAllowlist(revision=revision, userids=tuple(members))


def default_allowlist() -> ResumeReplacementAllowlist:
    return ResumeReplacementAllowlist(revision=1, userids=())


def get_allowlist(db: Session) -> ResumeReplacementAllowlist:
    row = db.query(SystemConfig).filter(SystemConfig.config_key == ROLLOUT_CONFIG_KEY).first()
    if row is None:
        return default_allowlist()
    try:
        return validate_allowlist(json.loads(row.config_value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BusinessException(50001, "resume_replacement_rollout_config_invalid") from exc


def update_allowlist(
    db: Session, *, expected_revision: int, userids: list[str], reason: str,
    operator: str,
) -> ResumeReplacementAllowlist:
    reason = str(reason).strip()
    if not 1 <= len(reason) <= 160:
        raise BusinessException(40101, "rollout_reason_invalid")
    if any(ord(char) < 32 for char in reason) or _SENSITIVE_REASON.search(reason):
        raise BusinessException(40101, "rollout_reason_sensitive_data_forbidden")
    row = db.query(SystemConfig).filter(
        SystemConfig.config_key == ROLLOUT_CONFIG_KEY
    ).with_for_update().first()
    current = get_allowlist(db) if row is not None else default_allowlist()
    if current.revision != expected_revision:
        raise BusinessException(40902, "rollout_revision_conflict", {
            "current_revision": current.revision,
        })
    try:
        updated = validate_allowlist({
            "revision": current.revision + 1,
            "userids": userids,
        })
    except ValueError as exc:
        raise BusinessException(40101, "rollout_allowlist_invalid") from exc
    value = json.dumps({
        "revision": updated.revision, "userids": list(updated.userids),
    }, ensure_ascii=False, separators=(",", ":"))
    if row is None:
        row = SystemConfig(
            config_key=ROLLOUT_CONFIG_KEY, config_value=value, value_type="json",
            description="简历全量更新灰度 allowlist", updated_by=operator,
        )
        db.add(row)
    else:
        row.config_value = value
        row.value_type = "json"
        row.updated_by = operator
    write_admin_log(
        db, target_type="system", target_id=ROLLOUT_CONFIG_KEY,
        action="manual_edit", operator=operator,
        before={"revision": current.revision, "member_count": len(current.userids)},
        after={"revision": updated.revision, "member_count": len(updated.userids)},
        reason=("resume_replacement_rollout_update:reason_sha256="
                + hashlib.sha256(reason.encode("utf-8")).hexdigest()[:16]),
    )
    db.commit()
    return updated
