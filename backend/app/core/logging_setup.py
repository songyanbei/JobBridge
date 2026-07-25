"""Process-level logging setup for machine-readable production telemetry."""
from __future__ import annotations

import os
import sys
import hashlib

from loguru import logger


def identifier_hash(value: str | None) -> str:
    """Stable non-reversible identifier for logs and metric dimensions."""
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:12]


def configure_loguru(app_env: str) -> None:
    """Emit bound ``log_event`` fields as JSON in production."""
    requested = os.getenv("LOG_FORMAT", "").strip().lower()
    serialize = requested == "json" or (
        not requested and app_env.strip().lower() == "production"
    )
    logger.remove()
    logger.add(
        sys.stderr,
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        serialize=serialize,
        backtrace=False,
        diagnose=False,
        enqueue=False,
    )
