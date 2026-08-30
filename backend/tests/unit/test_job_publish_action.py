from types import SimpleNamespace

import pytest

from app.schemas.conversation import SessionState
from app.services.action_gateway import ActionGateway
from app.services.upload_service import (
    confirm_job_draft, prepare_job_confirmation,
)
from app.wecom.callback import WeComMessage


def _session():
    return SessionState(
        profile="recruitment.job", role="factory",
        pending_upload_intent="upload_job",
        pending_upload={
            "city": "苏州", "job_category": "普工", "salary_floor_monthly": 5000,
            "pay_type": "月薪", "headcount": 2,
        },
        pending_operation_id="op-1",
    )


def test_confirm_job_gateway_uses_draft_without_classifier(monkeypatch):
    session = _session()
    monkeypatch.setattr("app.services.action_gateway.intent_service.classify_for_action_gateway", lambda **_: (_ for _ in ()).throw(AssertionError()))
    msg = WeComMessage(msg_id="m-1", turn_id="t-1", from_user="factory-1", msg_type="text", content="确认发布")
    envelope = ActionGateway(mode="on").classify(msg, session=session, actor=SimpleNamespace(role="factory"))
    assert envelope.action_name == "confirm_job"
    assert envelope.parse_ref is None


def test_confirm_job_nonce_and_digest_are_idempotent():
    session = _session()
    prepared = prepare_job_confirmation(session)
    assert prepare_job_confirmation(session).confirmation_nonce == prepared.confirmation_nonce
    confirmed = confirm_job_draft(session, confirmation_nonce=prepared.confirmation_nonce, draft_digest=prepared.draft_digest)
    assert confirmed.status == "confirmed"
    assert session.pending_action["confirmed"] is True
    with pytest.raises(ValueError, match="nonce"):
        confirm_job_draft(session, confirmation_nonce="stale", draft_digest=prepared.draft_digest)
