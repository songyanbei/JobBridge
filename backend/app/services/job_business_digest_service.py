"""Versioned, deterministic digest of non-lifecycle Job business fields."""
from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import date, datetime, timezone
from typing import Any

from app.services.storage_reference_service import normalize_storage_reference

DIGEST_VERSION = 1
DIGEST_FIELDS_V1 = (
    "owner_userid", "city", "job_category", "job_sub_category",
    "salary_floor_monthly", "salary_ceiling_monthly", "pay_type",
    "headcount", "gender_required", "age_min", "age_max", "is_long_term",
    "district", "address", "provide_meal", "provide_housing",
    "dorm_condition", "shift_pattern", "work_hours", "accept_couple",
    "accept_student", "accept_minority", "height_required",
    "experience_required", "education_required", "rebate",
    "employment_type", "contract_type", "min_duration", "raw_text",
    "description", "images", "miniprogram_url", "extra",
)


def _text(value: str) -> str:
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))


def _canonical(value: Any, *, field: str | None = None) -> Any:
    if field == "images":
        if value is None:
            return None
        if not isinstance(value, (list, tuple)):
            raise ValueError("images must be an array")
        return [normalize_storage_reference(item) for item in value]
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("extra object keys must be strings")
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        raise ValueError("floating point values are not allowed in job digests")
    raise ValueError(f"unsupported digest value: {type(value).__name__}")


def canonical_business_bytes(job: Any, *, digest_version: int = DIGEST_VERSION) -> bytes:
    if digest_version != 1:
        raise ValueError(f"unsupported digest version: {digest_version}")
    body = {
        field: _canonical(getattr(job, field, None), field=field)
        for field in DIGEST_FIELDS_V1
    }
    return json.dumps(
        body,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def business_digest(job: Any, version: int = DIGEST_VERSION, *, digest_version: int | None = None) -> str:
    selected = digest_version if digest_version is not None else version
    return hashlib.sha256(canonical_business_bytes(job, digest_version=selected)).hexdigest()
