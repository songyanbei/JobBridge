from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.models import WecomInboundEvent, WecomOutboundOutbox
from app.schemas.conversation import SessionState
from app.services import message_router
from app.services.worker import Worker
from app.wecom.callback import WeComMessage


def _group_message(content: str = "苏州找普工") -> WeComMessage:
    return WeComMessage(
        msg_id="aibot_group_msg",
        from_user="opaque-member",
        msg_type="text",
        content=content,
        source_channel="wecom_aibot",
        conversation_type="group",
        conversation_id="chat-123",
        chat_id="chat-123",
        ordering_key="wecom:wecom_aibot:group:chat-123",
        actor_id_kind="opaque",
    )


def test_group_routes_to_chat_session_and_fail_closes_business_capabilities():
    identity = SimpleNamespace(identity_status="verified", mapped_external_userid="member-1")
    session = SessionState(role="worker", active_flow="idle")
    user_context = SimpleNamespace(status="active", role="worker", should_welcome=False)
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = identity

    with patch.object(message_router.user_service, "identify_or_register", return_value=user_context), \
        patch.object(message_router.user_service, "update_last_active"), \
        patch.object(message_router.conversation_service, "load_session", return_value=None) as load, \
        patch.object(message_router.conversation_service, "create_session", return_value=session), \
        patch.object(message_router.conversation_service, "save_session") as save:
        replies = message_router.process(_group_message(), db)

    assert len(replies) == 1
    assert "单聊" in replies[0].content
    load.assert_called_once_with("wecom:aibot:group:chat-123")
    save.assert_called_once_with("wecom:aibot:group:chat-123", session)
    assert not session.search_criteria


class _OutboxDb:
    def __init__(self, inbound):
        self.inbound = inbound
        self.added = []

    def get(self, model, _event_id):
        assert model is WecomInboundEvent
        return self.inbound

    def add(self, value):
        self.added.append(value)


def test_group_outbox_has_chat_target_and_no_user_target():
    inbound = SimpleNamespace(
        source_channel="wecom_aibot",
        conversation_type="group",
        conversation_id="chat-123",
        chat_id="chat-123",
        ordering_key="wecom:wecom_aibot:group:chat-123",
        provider_req_id="req-1",
    )
    db = _OutboxDb(inbound)
    worker = object.__new__(Worker)
    reply = SimpleNamespace(
        userid="member-1", msg_type="text", content="群聊暂不支持搜索",
        intent=None, criteria_snapshot=None, recommendation_context=None,
        recommendation_request=None,
    )

    worker._stage_outbox(db, 7, [reply])

    assert len(db.added) == 1
    row = db.added[0]
    assert isinstance(row, WecomOutboundOutbox)
    assert row.userid is None
    assert row.conversation_id == "chat-123"
    assert row.chat_id == "chat-123"
    assert row.ordering_key == "wecom:wecom_aibot:group:chat-123"
