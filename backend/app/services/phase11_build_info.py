"""Stable API/worker build-probe contract used by the phase11 runner."""
from __future__ import annotations

from app.config import settings


PHASE11_CAPABILITIES: tuple[str, ...] = (
    "resume_nullable_dto",
    "resume_lifecycle_double_write",
)


def build_probe_payload() -> dict:
    return {
        "build_number": settings.phase11_build_number,
        "build_sha": settings.phase11_build_sha,
        "capabilities": list(PHASE11_CAPABILITIES),
    }
