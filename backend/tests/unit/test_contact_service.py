from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import ContactAccessAudit, ContactDelivery, ContactGrant, ContactRequest
from app.models import WecomInboundEvent
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


def test_direction_bound_grant_requires_matching_direction_at_redeem():
    db = _db()
    service = ContactService(db, mode="on")
    request = service.create_contact_request(
        "actor-1", "recruitment.resume:1", direction="search_worker",
        listing_version=3, policy_version="matching-policy-v1",
    )
    grant = service.issue_one_time_grant(
        request.request_id, "actor-1", "recruitment.resume:1",
        direction="search_worker", listing_version=3,
        policy_version="matching-policy-v1",
    )
    denied = service.redeem_grant(
        grant.grant_id, grant.token, "actor-1", current_direction="search_job",
        current_listing_version=3, current_policy_version="matching-policy-v1",
        listing_status="passed",
    )
    assert denied.code == "forbidden"
    allowed = service.redeem_grant(
        grant.grant_id, grant.token, "actor-1", current_direction="search_worker",
        current_listing_version=3, current_policy_version="matching-policy-v1",
        listing_status="passed",
    )
    assert allowed.success


def test_group_contact_redeem_is_fail_closed_before_consuming_grant():
    db = _db()
    service = ContactService(db, mode="on")
    request = service.create_contact_request("actor-1", "recruitment.job:1")
    grant = service.issue_one_time_grant(request.request_id, "actor-1", "recruitment.job:1")
    real_get = db.get
    db.get = lambda entity, key: (
        SimpleNamespace(
            conversation_type="group", source_channel="wecom_aibot",
            conversation_id="chat-1", chat_id="chat-1",
        )
        if entity is WecomInboundEvent else real_get(entity, key)
    )

    result = service.redeem_grant(
        grant.grant_id, grant.token, "actor-1", inbound_event_id=42,
        userid="actor-1",
    )

    assert not result.success
    assert result.code == "forbidden"
    assert db.query(ContactGrant).one().status == "issued"
    assert db.query(ContactDelivery).count() == 0
