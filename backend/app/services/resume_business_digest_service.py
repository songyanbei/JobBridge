"""Versioned deterministic digest of resume business fields."""
from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import date, datetime, timezone

from app.services.storage_reference_service import normalize_storage_reference

DIGEST_VERSION = 1
DIGEST_FIELDS = (
    "owner_userid", "expected_cities", "expected_job_categories",
    "salary_expect_floor_monthly", "gender", "age", "accept_long_term",
    "accept_short_term", "expected_districts", "height", "weight", "education",
    "work_experience", "accept_night_shift", "accept_standing_work",
    "accept_overtime", "accept_outside_province", "couple_seeking_together",
    "has_health_certificate", "ethnicity", "available_from", "has_tattoo",
    "taboo", "raw_text", "description", "images", "miniprogram_url", "extra",
)


def _canonical(value, *, media: bool = False):
    if media:
        return None if value is None else [normalize_storage_reference(v) for v in value]
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    if isinstance(value, dict):
        return {k: _canonical(value[k]) for k in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if isinstance(value, datetime):
        value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if value is None or isinstance(value, (bool, int)):
        return value
    raise ValueError(f"unsupported digest value: {type(value).__name__}")


def business_digest(resume, *, digest_version: int = DIGEST_VERSION) -> str:
    if digest_version != DIGEST_VERSION:
        raise ValueError("unsupported resume digest version")
    body = {field: _canonical(getattr(resume, field, None), media=field == "images") for field in DIGEST_FIELDS}
    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(payload).hexdigest()
