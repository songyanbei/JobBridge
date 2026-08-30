"""Resume publish permissions, action allowlist and redacted confirmation."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from app.listing.resume_profile import RESUME_PROFILE

RESUME_ACTIONS = frozenset({"publish_resume", "replace_resume", "confirm_resume", "edit_resume_draft", "delist_resume", "restore_resume"})


@dataclass(frozen=True)
class ResumePublishPolicy:
    version: str = "resume-publish-policy-v1"
    enabled: bool = False
    action_allowlist: frozenset[str] = RESUME_ACTIONS

    def authorize(self, *, actor_role: str, action: str, owner_userid: str, actor_userid: str) -> bool:
        return actor_role == "worker" and action in self.action_allowlist and str(owner_userid) == str(actor_userid)

    def validate_slots(self, values: dict) -> tuple[str, ...]:
        return RESUME_PROFILE.missing_hard_fields(values)

    def confirmation(self, values: dict) -> dict:
        redacted = RESUME_PROFILE.redact(values)
        body = json.dumps(redacted, ensure_ascii=True, sort_keys=True, default=str)
        return {"profile": RESUME_PROFILE.name, "policy_version": self.version, "fields": redacted, "digest": hashlib.sha256(body.encode()).hexdigest()}

