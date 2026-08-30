"""Opaque Contact domain DTOs.

These contracts intentionally have no phone/wechat/contact-value fields. The
only module allowed to handle a contact value is the server-side Contact
service, after authorization.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ContactRequestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor_id: str = Field(min_length=1, max_length=64)
    listing_ref: str = Field(min_length=1, max_length=200)
    action: Literal["request_contact"] = "request_contact"
    listing_version: int | None = Field(default=None, ge=0)
    policy_version: str | None = Field(default=None, max_length=64)
    trace_id: str | None = Field(default=None, max_length=64)


class ContactRequestView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=64)
    listing_ref: str = Field(min_length=1, max_length=200)
    action: Literal["request_contact"]
    status: Literal["pending", "authorized", "revoked", "expired"]
    expires_at: datetime
    trace_id: str | None = None


class ContactGrantMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grant_id: str = Field(min_length=1, max_length=64)
    token: str = Field(min_length=20, max_length=256)
    token_type: Literal["one_time"] = "one_time"
    expires_at: datetime
    channel: Literal["platform_request", "wecom"] = "platform_request"


class ContactRedeemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor_id: str = Field(min_length=1, max_length=64)
    grant_id: str = Field(min_length=1, max_length=64)
    token: str = Field(min_length=20, max_length=256)
    trace_id: str | None = Field(default=None, max_length=64)


class ContactResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool
    code: Literal[
        "ok", "contact_unavailable", "invalid_grant", "forbidden",
        "expired", "revoked", "already_used", "rate_limited",
    ]
    grant: ContactGrantMetadata | None = None
    message: str = Field(default="暂时无法提供联系方式，请稍后重试。", max_length=200)


def validate_opaque_id(value: str) -> str:
    """Normalize IDs at API boundaries without attempting to decode them."""
    normalized = str(value or "").strip()
    if not normalized or any(ch.isspace() for ch in normalized):
        raise ValueError("opaque id must be non-empty and contain no whitespace")
    return normalized


for _model in (ContactRequestCreate, ContactRequestView, ContactGrantMetadata, ContactRedeemRequest):
    _model.model_fields.get("actor_id")

