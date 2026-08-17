"""Strict parser for the hidden database-backed replacement allowlist."""
from __future__ import annotations

from dataclasses import dataclass

ROLLOUT_CONFIG_KEY = "resume.replacement.rollout.allowlist"
# A short-lived pre-freeze build wrote this spelling.  Migration validation
# must fail closed when it is present, even though new writes use only the
# canonical public-contract key above.
LEGACY_ROLLOUT_CONFIG_KEYS = ("rollout.resume_replacement.allowlist",)
MAX_ALLOWLIST_REVISION = 18_446_744_073_709_551_615


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
