from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import ContactAccessAudit, ContactDelivery, ContactGrant, ContactRequest
from app.listing.contact import CONTACT_UNAVAILABLE_MESSAGE, ContactService


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[
        ContactRequest.__table__, ContactGrant.__table__, ContactAccessAudit.__table__, ContactDelivery.__table__,
    ])
    return Session(engine)


def test_off_mode_is_stable_and_does_not_issue_or_redeem():
    db = _db()
    service = ContactService(db, mode="off")
    request = service.create_contact_request("actor-1", "recruitment.job:1")
    result = service.issue_one_time_grant(request.request_id, "actor-1", "recruitment.job:1")
    assert result.code == "contact_unavailable"
    assert result.message == CONTACT_UNAVAILABLE_MESSAGE
    assert db.query(ContactGrant).count() == 0


def test_redeem_consumes_once_and_creates_one_delivery():
    db = _db()
    service = ContactService(db, mode="on", rate_limit=3)
    request = service.create_contact_request("actor-1", "recruitment.job:1")
    grant = service.issue_one_time_grant(request.request_id, "actor-1", "recruitment.job:1")
    assert grant.token
    first = service.redeem_grant(grant.grant_id, grant.token, "actor-1")
    second = service.redeem_grant(grant.grant_id, grant.token, "actor-1")
    assert first.success and first.code == "ok"
    assert not second.success and second.code == "already_used"
    assert db.query(ContactDelivery).count() == 1
    assert db.query(ContactGrant).one().used_at is not None


def test_cross_actor_and_expired_grants_are_denied_without_contact_value():
    db = _db()
    service = ContactService(db, mode="on")
    request = service.create_contact_request("actor-1", "recruitment.job:1")
    grant = service.issue_one_time_grant(request.request_id, "actor-1", "recruitment.job:1")
    denied = service.redeem_grant(grant.grant_id, grant.token, "actor-2")
    assert denied.code == "forbidden"
    row = db.query(ContactGrant).one()
    row.expires_at = datetime.utcnow() - timedelta(seconds=1)
    db.flush()
    expired = service.redeem_grant(grant.grant_id, grant.token, "actor-1")
    assert expired.code == "expired"
