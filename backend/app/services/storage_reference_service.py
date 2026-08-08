"""Normalize persisted storage references to canonical object keys."""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import unquote, urlsplit

from app.config import settings

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_BAD_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


def _trusted_origins() -> set[str]:
    return {
        value.strip().rstrip("/").lower()
        for value in settings.oss_trusted_origins.split(",")
        if value.strip()
    }


def normalize_storage_reference(raw_value: str) -> str:
    if not isinstance(raw_value, str) or not raw_value or _CONTROL.search(raw_value):
        raise ValueError("invalid_media_reference")
    if "\\" in raw_value or _WINDOWS_DRIVE.match(raw_value):
        raise ValueError("unsafe_media_path")

    parsed = urlsplit(raw_value)
    prefix = "/" + settings.oss_local_url_prefix.strip("/")
    if parsed.scheme or parsed.netloc:
        if parsed.scheme.lower() not in {"http", "https"}:
            raise ValueError("unsupported_media_url_scheme")
        origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
        if origin not in _trusted_origins():
            raise ValueError("external_url")
        raw_path = parsed.path
        if prefix != "/" and not (raw_path == prefix or raw_path.startswith(prefix + "/")):
            raise ValueError("untrusted_media_url_prefix")
    elif raw_value.startswith("/"):
        raw_path = parsed.path
        if prefix == "/" or not raw_path.startswith(prefix + "/"):
            raise ValueError("untrusted_media_url_prefix")
    else:
        if parsed.query or parsed.fragment:
            raise ValueError("query_on_raw_object_key")
        raw_path = parsed.path

    if _BAD_ESCAPE.search(raw_path):
        raise ValueError("invalid_percent_escape")
    decoded = unicodedata.normalize("NFC", unquote(raw_path, errors="strict"))
    if "%" in decoded:
        raise ValueError("ambiguous_encoded_media_path")
    if decoded.startswith(prefix + "/"):
        decoded = decoded[len(prefix) + 1 :]
    elif decoded.startswith("/"):
        decoded = decoded[1:]

    parts = decoded.split("/")
    if not decoded or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("unsafe_media_path")
    if _WINDOWS_DRIVE.match(decoded) or "\\" in decoded or _CONTROL.search(decoded):
        raise ValueError("unsafe_media_path")
    return "/".join(parts)


normalize_object_key = normalize_storage_reference
