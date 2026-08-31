from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services import message_router
from app.wecom.callback import WeComMessage


def _message() -> WeComMessage:
    return WeComMessage(
        msg_id="aibot_msg",
        from_user="opaque-actor",
        msg_type="event",
        source_channel="wecom_aibot",
        conversation_type="single",
        conversation_id="opaque-actor",
        actor_id_kind="opaque",
    )


def _db(identity):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = identity
    return db


def test_unverified_opaque_actor_gets_safe_reply_without_registration():
    identity = SimpleNamespace(identity_status="unverified", mapped_external_userid=None)
    db = _db(identity)
    with patch.object(message_router.user_service, "identify_or_register") as identify:
        replies = message_router.process(_message(), db)

    identify.assert_not_called()
    assert len(replies) == 1
    assert "绑定" in replies[0].content


def test_verified_opaque_actor_routes_as_mapped_user():
    identity = SimpleNamespace(identity_status="verified", mapped_external_userid="canonical-user")
    db = _db(identity)
    user_context = SimpleNamespace(
        status="active",
        role="worker",
        external_userid="canonical-user",
        should_welcome=False,
    )
    with patch.object(
        message_router.user_service,
        "identify_or_register",
        return_value=user_context,
    ) as identify:
        replies = message_router.process(_message(), db)

    identify.assert_called_once_with("canonical-user", db)
    assert replies == []
