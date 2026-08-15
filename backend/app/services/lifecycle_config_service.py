"""Single source of truth for job lifecycle configuration."""
from __future__ import annotations

import logging
from threading import Lock
from time import monotonic

from sqlalchemy.orm import Session

from app.models import SystemConfig

logger = logging.getLogger(__name__)
JOB_TTL_DEFAULT_DAYS = 30
JOB_CANDIDATE_TTL_DEFAULT_DAYS = 7
HARD_DELETE_DELAY_DEFAULT_DAYS = 7
MISSING_CONFIG_WARNING_INTERVAL_SECONDS = 300

_missing_warning_lock = Lock()
_missing_warning_last_at: dict[str, float] = {}


def _warn_missing_config(key: str, raw, fallback: int) -> None:
    now = monotonic()
    with _missing_warning_lock:
        last_at = _missing_warning_last_at.get(key)
        if (
            last_at is not None
            and now - last_at < MISSING_CONFIG_WARNING_INTERVAL_SECONDS
        ):
            return
        _missing_warning_last_at[key] = now
    logger.warning(
        "missing_lifecycle_config key=%s value=%r fallback=%s",
        key,
        raw,
        fallback,
    )


def _mark_config_recovered(key: str) -> None:
    with _missing_warning_lock:
        _missing_warning_last_at.pop(key, None)


def _read(db: Session, key: str, default: int, lower: int, upper: int) -> int:
    row = db.query(SystemConfig).filter(SystemConfig.config_key == key).first()
    raw = getattr(row, "config_value", None)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    if not lower <= value <= upper:
        value = default
    if row is None:
        _warn_missing_config(key, raw, value)
    elif str(raw) != str(value):
        logger.warning("invalid_lifecycle_config key=%s value=%r fallback=%s", key, raw, value)
    else:
        _mark_config_recovered(key)
    return value


def get_job_ttl_days(db: Session) -> int:
    return _read(db, "ttl.job.days", JOB_TTL_DEFAULT_DAYS, 1, 3650)


def get_job_candidate_ttl_days(db: Session) -> int:
    return _read(db, "ttl.job.candidate.days", JOB_CANDIDATE_TTL_DEFAULT_DAYS, 1, 365)


def get_hard_delete_delay_days(db: Session) -> int:
    return _read(
        db,
        "ttl.hard_delete.delay_days",
        HARD_DELETE_DELAY_DEFAULT_DAYS,
        0,
        3650,
    )
