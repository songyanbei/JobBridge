"""Single source of truth for job lifecycle configuration."""
from __future__ import annotations

import logging
from sqlalchemy.orm import Session

from app.models import SystemConfig

logger = logging.getLogger(__name__)
JOB_TTL_DEFAULT_DAYS = 30
JOB_CANDIDATE_TTL_DEFAULT_DAYS = 7


def _read(db: Session, key: str, default: int, lower: int, upper: int) -> int:
    row = db.query(SystemConfig).filter(SystemConfig.config_key == key).first()
    raw = getattr(row, "config_value", None)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    if not lower <= value <= upper:
        value = default
    if raw is not None and str(raw) != str(value):
        logger.warning("invalid_lifecycle_config key=%s value=%r fallback=%s", key, raw, value)
    return value


def get_job_ttl_days(db: Session) -> int:
    return _read(db, "ttl.job.days", JOB_TTL_DEFAULT_DAYS, 1, 3650)


def get_job_candidate_ttl_days(db: Session) -> int:
    return _read(db, "ttl.job.candidate.days", JOB_CANDIDATE_TTL_DEFAULT_DAYS, 1, 365)
