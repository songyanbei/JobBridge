"""Fail-closed identity lookup for AIBot business routing."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import WecomAibotIdentity


@dataclass(frozen=True)
class AibotIdentityResolution:
    """The only identity state the Worker/Router boundary needs."""

    status: str = "unverified"
    mapped_external_userid: str | None = None

    @property
    def verified(self) -> bool:
        return self.status == "verified" and bool(self.mapped_external_userid)


def resolve_aibot_identity(db: Session, opaque_actor_id: str) -> AibotIdentityResolution:
    """Read the durable mapping; missing and malformed rows are unverified."""
    if not opaque_actor_id:
        return AibotIdentityResolution()
    row = db.query(WecomAibotIdentity).filter(
        WecomAibotIdentity.opaque_actor_id == opaque_actor_id,
    ).first()
    if row is None:
        return AibotIdentityResolution()
    status = str(row.identity_status or "unverified")
    mapped = row.mapped_external_userid
    if status != "verified" or not isinstance(mapped, str) or not mapped.strip():
        return AibotIdentityResolution(status=status)
    return AibotIdentityResolution(status=status, mapped_external_userid=mapped.strip())
