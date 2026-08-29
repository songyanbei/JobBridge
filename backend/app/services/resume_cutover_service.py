"""Small, executable primitives for the phase11 write barrier and watermark."""
from __future__ import annotations

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.config import settings
from app.core.exceptions import BusinessException
from app.models import Resume


def assert_resume_writes_allowed() -> None:
    """New writers honor the operational barrier; the gateway still drains old builds."""
    if settings.phase11_resume_writes_paused:
        raise BusinessException(50301, "resume_writes_paused_for_cutover")


def capture_cutover_watermark(db: Session) -> int:
    """Capture MAX(resume.id) only while the externally enforced barrier is active."""
    if not settings.phase11_resume_writes_paused:
        raise RuntimeError("resume_write_barrier_not_active")
    return int(db.query(func.max(Resume.id)).scalar() or 0)


def lifecycle_anomaly_counts(db: Session, *, after_id: int = 0) -> dict[str, int]:
    """Count only the two compatibility-writer invariants after a watermark."""
    base = (Resume.id > int(after_id), Resume.deleted_at.is_(None))
    passed_invalid = db.query(func.count(Resume.id)).filter(
        *base,
        Resume.audit_status == "passed",
        or_(
            Resume.activated_at.is_(None),
            Resume.expires_at.is_(None),
            Resume.candidate_expires_at.isnot(None),
        ),
    ).scalar()
    candidate_invalid = db.query(func.count(Resume.id)).filter(
        *base,
        Resume.audit_status.in_(("pending", "rejected")),
        or_(
            Resume.activated_at.isnot(None),
            Resume.expires_at.isnot(None),
            Resume.candidate_expires_at.is_(None),
        ),
    ).scalar()
    return {
        "passed_invalid": int(passed_invalid or 0),
        "candidate_invalid": int(candidate_invalid or 0),
    }


def assert_lifecycle_invariants(db: Session, *, after_id: int = 0) -> dict[str, int]:
    counts = lifecycle_anomaly_counts(db, after_id=after_id)
    if any(counts.values()):
        raise RuntimeError("resume_lifecycle_incremental_invariant_failed")
    return counts
