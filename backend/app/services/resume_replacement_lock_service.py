"""Minimal lock entry point for replacement candidate creation."""
from sqlalchemy.orm import Session

from app.models import Resume, ResumeReplacement, User


def lock_replacement_creation(
    db: Session, owner_userid: str, old_resume_id: int,
    operation_id: str, source_msg_id: str,
):
    identity = (
        (ResumeReplacement.operation_id == operation_id)
        | (ResumeReplacement.source_msg_id == source_msg_id)
    )
    existing = db.query(ResumeReplacement).filter(identity).first()
    if existing is not None:
        return existing, None
    # All candidate creation follows owner -> old Resume -> relation.  The
    # owner lock also serializes two requests that name different old resumes
    # but replay the same source message.
    db.query(User).filter(User.external_userid == owner_userid).with_for_update().one()
    old = db.query(Resume).populate_existing().filter(Resume.id == old_resume_id).with_for_update().first()
    existing = db.query(ResumeReplacement).populate_existing().filter(identity).with_for_update().first()
    if existing is not None:
        return existing, old
    active = db.query(ResumeReplacement).populate_existing().filter(
        ResumeReplacement.active_old_resume_id == old_resume_id
    ).with_for_update().first()
    return (active, old) if active is not None else (None, old)
