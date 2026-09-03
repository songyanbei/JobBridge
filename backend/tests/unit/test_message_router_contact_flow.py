from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import (
    ContactAccessAudit,
    ContactDelivery,
    ContactGrant,
    ContactRequest,
    Job,
    Resume,
    WecomInboundEvent,
    WecomOutboundOutbox,
)
from app.schemas.conversation import CandidateSnapshot, SessionState
from app.services import message_router
from app.services.user_service import UserContext
from app.wecom.callback import WeComMessage


def _worker_context(userid: str) -> UserContext:
    return UserContext(
        external_userid=userid,
        role="worker",
        status="active",
        display_name=None,
        company=None,
        contact_person=None,
        phone=None,
        can_search_jobs=True,
        can_search_workers=False,
        is_first_touch=False,
        should_welcome=False,
    )


def test_contact_command_runs_request_grant_redeem_and_stages_contact_outbox(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[
        ContactRequest.__table__,
        ContactGrant.__table__,
        ContactDelivery.__table__, ContactAccessAudit.__table__,
    ])
    db = Session(engine)
    userid = "router-contact-worker"
    captured_outbox = []
    real_add = db.add

    def add(obj, *args, **kwargs):
        if isinstance(obj, WecomOutboundOutbox):
            captured_outbox.append(obj)
            return None
        return real_add(obj, *args, **kwargs)

    monkeypatch.setattr(db, "add", add)
    job = SimpleNamespace(
        id=1, audit_status="passed", deleted_at=None,
        expires_at=None, version=3,
    )
    real_query = db.query

    def query(entity, *args, **kwargs):
        if entity is Job:
            class _JobQuery:
                def filter(self, *_args, **_kwargs):
                    return self

                def first(self):
                    return job

            return _JobQuery()
        return real_query(entity, *args, **kwargs)

    monkeypatch.setattr(db, "query", query)

    session = SessionState(
        role="worker",
        active_flow="search_active",
        candidate_snapshot=CandidateSnapshot(
            candidate_ids=["1"], snapshot_id="snap-contact-router",
        ),
        shown_items=["1"],
    )
    monkeypatch.setattr(message_router._settings_module, "contact_service_mode", "on")
    monkeypatch.setattr(message_router.conversation_service, "load_session", lambda _: session)
    monkeypatch.setattr(message_router.conversation_service, "save_session", lambda *_: None)
    monkeypatch.setattr(message_router.user_service, "update_last_active", lambda *_: None)
    monkeypatch.setattr(
        message_router,
        "classify_intent",
        lambda **_: (_ for _ in ()).throw(AssertionError("contact must not invoke classifier")),
    )

    replies = message_router.process(
        WeComMessage(
            msg_id="msg-contact-router",
            turn_id="turn-contact-router",
            from_user=userid,
            msg_type="text",
            content="联系",
        ),
        db,
        user_context=_worker_context(userid),
        inbound_event_id=101,
    )

    assert replies == []
    request = db.query(ContactRequest).one()
    delivery = db.query(ContactDelivery).one()
    outbox = captured_outbox[0]
    assert request.listing_ref == "recruitment.job:1"
    assert request.listing_version == 3
    assert delivery.grant_id
    assert delivery.channel == "platform_request"
    assert delivery.status == "prepared"
    assert outbox.inbound_event_id == 101
    assert outbox.contact_delivery_id == delivery.delivery_id
    assert outbox.recommendation_delivery_id is None
    assert outbox.content is None


def test_contact_command_without_search_context_is_guidance(monkeypatch):
    session = SessionState(role="worker", active_flow="idle")
    monkeypatch.setattr(message_router.conversation_service, "load_session", lambda _: session)
    monkeypatch.setattr(message_router.conversation_service, "save_session", lambda *_: None)
    monkeypatch.setattr(message_router.user_service, "update_last_active", lambda *_: None)
    replies = message_router.process(
        WeComMessage(from_user="worker-no-search", msg_type="text", content="联系"),
        object(),
        user_context=_worker_context("worker-no-search"),
    )
    assert len(replies) == 1
    assert "先搜索岗位" in replies[0].content


def test_aibot_contact_redeem_inherits_conversation_metadata():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[
        ContactRequest.__table__, ContactGrant.__table__,
        ContactDelivery.__table__, ContactAccessAudit.__table__,
    ])
    db = Session(engine)
    inbound = SimpleNamespace(
        id=301,
        msg_id="aibot-contact-msg",
        turn_id="aibot-contact-turn",
        source_channel="wecom_aibot",
        provider_msg_id="provider-contact-msg",
        from_userid="opaque-actor",
        conversation_type="single",
        conversation_id="opaque-actor",
        ordering_key="wecom:wecom_aibot:single:opaque-actor",
        provider_req_id="req-contact",
        msg_type="text",
        status="done",
    )
    captured_outbox = []
    real_add = db.add

    def add(obj, *args, **kwargs):
        if isinstance(obj, WecomOutboundOutbox):
            captured_outbox.append(obj)
            return None
        return real_add(obj, *args, **kwargs)

    db.add = add
    real_get = db.get
    db.get = lambda entity, key: inbound if entity is WecomInboundEvent else real_get(entity, key)

    service = __import__("app.listing.contact", fromlist=["ContactService"]).ContactService(
        db, mode="on", rate_limit=3,
    )
    request = service.create_contact_request("actor-1", "recruitment.job:1")
    grant = service.issue_one_time_grant(request.request_id, "actor-1", "recruitment.job:1")
    result = service.redeem_grant(
        grant.grant_id, grant.token, "actor-1", inbound_event_id=inbound.id,
        userid="actor-1",
    )

    assert result.success
    outbox = captured_outbox[0]
    assert outbox.channel == "wecom_aibot"
    assert outbox.conversation_type == "single"
    assert outbox.conversation_id == "opaque-actor"
    assert outbox.ordering_key == inbound.ordering_key
    assert outbox.provider_req_id == "req-contact"
    assert outbox.reply_command == "aibot_respond_msg"
    assert outbox.stream_id
    assert outbox.finish is True
    assert outbox.reply_expires_at is not None


def test_resume_contact_command_uses_explicit_resume_ref_and_direction(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[
        ContactRequest.__table__, ContactGrant.__table__,
        ContactDelivery.__table__, ContactAccessAudit.__table__,
    ])
    db = Session(engine)
    resume = SimpleNamespace(
        id=7, audit_status="passed", deleted_at=None, delist_reason=None,
        expires_at=None, aggregate_version=4, version=4,
    )
    real_query = db.query

    def query(entity, *args, **kwargs):
        if entity is Resume:
            class _ResumeQuery:
                def filter(self, *_args, **_kwargs):
                    return self

                def first(self):
                    return resume

            return _ResumeQuery()
        return real_query(entity, *args, **kwargs)

    monkeypatch.setattr(db, "query", query)
    monkeypatch.setattr(message_router._settings_module, "contact_service_mode", "on")
    session = SessionState(
        role="employer", active_flow="search_active",
        candidate_snapshot=CandidateSnapshot(
            candidate_ids=["recruitment.resume:7"], snapshot_id="snap-resume-contact",
        ),
        shown_items=["recruitment.resume:7"],
        profile="recruitment.resume",
    )
    result = message_router._try_handle_contact(
        "联系", WeComMessage(from_user="employer-1", msg_type="text", content="联系"),
        SimpleNamespace(status="active"), session, db,
    )

    assert result and result[0].content == "联系请求已提交。"
    request = db.query(ContactRequest).one()
    assert request.listing_ref == "recruitment.resume:7"
    assert request.listing_version == 4
    assert request.policy_version == message_router._settings_module.resume_matching_policy_version
    assert request.direction == "search_worker"


def test_contact_ref_rejects_cross_direction_shown_item():
    session = SessionState(
        role="employer", profile="recruitment.resume", shown_items=["recruitment.job:9"],
    )
    assert message_router._contact_listing_ref("联系", session) == ""


def test_contact_ref_uses_search_worker_direction_with_legacy_job_profile():
    session = SessionState(
        role="employer", profile="recruitment.job", last_intent="search_worker",
        shown_items=["97"],
    )
    assert message_router._contact_listing_ref("联系1", session) == "recruitment.resume:97"
