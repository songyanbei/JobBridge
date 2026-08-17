"""The only lock entry points for resume replacement transitions."""
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Resume, ResumeReplacement, User


@dataclass(frozen=True)
class ReplacementGraphHint:
    id: int
    old_resume_id: int
    new_resume_id: int
    operation_id: str


def _hint(row) -> ReplacementGraphHint | None:
    if row is None:
        return None
    return ReplacementGraphHint(
        int(row.id), int(row.old_resume_id), int(row.new_resume_id), str(row.operation_id),
    )


def current_active_replacement_hint(db: Session, old_resume_id: int) -> ReplacementGraphHint | None:
    statement = select(
        ResumeReplacement.id, ResumeReplacement.old_resume_id,
        ResumeReplacement.new_resume_id, ResumeReplacement.operation_id,
    ).where(ResumeReplacement.active_old_resume_id == old_resume_id)
    bind = db.get_bind()
    if bind.dialect.name == "sqlite":
        row = db.execute(statement).first()
    else:
        engine = getattr(bind, "engine", bind)
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            row = connection.execute(statement).first()
    return _hint(row)


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


def lock_replacement_graph(
    db: Session, replacement_id: int, *, hint: ReplacementGraphHint | None = None,
):
    if hint is None:
        hint = _hint(db.query(ResumeReplacement).filter(ResumeReplacement.id == replacement_id).first())
    if hint is None:
        return None, [], None
    if int(replacement_id) != hint.id:
        raise RuntimeError("replacement graph hint id mismatch")
    ids = sorted({hint.old_resume_id, hint.new_resume_id})
    resumes = (
        db.query(Resume).populate_existing().filter(Resume.id.in_(ids))
        .order_by(Resume.id).with_for_update().all()
    )
    relation = (
        db.query(ResumeReplacement).populate_existing()
        .filter(ResumeReplacement.id == replacement_id).with_for_update().one()
    )
    if (relation.old_resume_id, relation.new_resume_id, relation.operation_id) != (
        hint.old_resume_id, hint.new_resume_id, hint.operation_id,
    ):
        raise RuntimeError("replacement graph changed while locking")
    return relation, resumes, {resume.id: resume for resume in resumes}
