from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.llm.base import IntentResult
from app.schemas.conversation import SessionState
from app.services import message_router
from app.services.dialogue_reducer import DialogueDecision
from app.wecom.callback import WeComMessage


def _message(content: str) -> WeComMessage:
    return WeComMessage(
        msg_id="expired-upload-message",
        from_user="factory-expired-upload",
        msg_type="text",
        content=content,
        media_id=None,
        create_time=1_700_000_000,
    )


def _image_message(*, expired_upload_draft: bool = False) -> WeComMessage:
    return WeComMessage(
        msg_id="expired-upload-image",
        from_user="factory-expired-upload",
        msg_type="image",
        content="",
        media_id="wecom-expired-image",
        create_time=1_700_000_000,
        expired_upload_draft=expired_upload_draft,
    )


def _expired_session(active_flow: str) -> SessionState:
    return SessionState(
        role="factory",
        active_flow=active_flow,
        pending_upload={"city": "苏州", "job_category": "电子厂"},
        pending_upload_intent="upload_job",
        pending_expires_at=(
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat(),
        pending_upload_media_ids=[41, 42],
        pending_interruption={"intent": "search_worker"},
    )


@pytest.mark.parametrize(
    ("source", "active_flow"),
    [
        ("v2_dual_read", "upload_collecting"),
        ("v2_primary", "upload_conflict"),
    ],
)
def test_v2_clarification_cannot_preserve_expired_upload(
    monkeypatch, source, active_flow,
):
    session = _expired_session(active_flow)
    decision = DialogueDecision(
        dialogue_act="clarify",
        resolved_frame="job_upload",
        route_intent="follow_up",
        clarification={"kind": "llm_requested"},
    )
    route = SimpleNamespace(
        source=source,
        intent_result=IntentResult(intent="follow_up", structured_data={}),
        decision=decision,
        parse_result=None,
    )
    db = MagicMock()
    mark_delete_pending = MagicMock()

    monkeypatch.setattr(message_router._settings_module, "dialogue_v2_mode", "primary")
    monkeypatch.setattr(message_router, "classify_dialogue", lambda **_kwargs: route)
    monkeypatch.setattr(
        message_router.conversation_service, "load_session", lambda _userid: session,
    )
    monkeypatch.setattr(
        message_router.conversation_service, "save_session", MagicMock(),
    )
    monkeypatch.setattr(
        "app.services.job_media_service.mark_delete_pending", mark_delete_pending,
    )

    replies = message_router._handle_text(
        _message("这个字段怎么填"),
        MagicMock(role="factory", should_welcome=False),
        db,
    )

    assert replies
    mark_delete_pending.assert_called_once_with(db, [41, 42])
    assert session.pending_upload == {}
    assert session.pending_upload_intent is None
    assert session.pending_upload_media_ids == []
    assert session.pending_interruption is None
    assert session.active_flow == "idle"


def test_v2_patch_short_return_expires_before_classification(monkeypatch):
    session = _expired_session("upload_collecting")
    classify_dialogue = MagicMock()
    db = MagicMock()
    mark_delete_pending = MagicMock()

    monkeypatch.setattr(message_router._settings_module, "dialogue_v2_mode", "primary")
    monkeypatch.setattr(message_router, "classify_dialogue", classify_dialogue)
    monkeypatch.setattr(
        message_router.conversation_service, "load_session", lambda _userid: session,
    )
    monkeypatch.setattr(
        message_router.conversation_service, "save_session", MagicMock(),
    )
    monkeypatch.setattr(
        "app.services.job_media_service.mark_delete_pending", mark_delete_pending,
    )

    replies = message_router._handle_text(
        _message("2个人"),
        MagicMock(role="factory", should_welcome=False),
        db,
    )

    assert replies[0].content == message_router.PENDING_EXPIRED_REPLY
    classify_dialogue.assert_not_called()
    mark_delete_pending.assert_called_once_with(db, [41, 42])
    assert session.pending_upload_intent is None
    assert session.pending_upload_media_ids == []


def test_conflict_handler_cannot_resume_expired_upload(monkeypatch):
    session = _expired_session("upload_conflict")
    db = MagicMock()
    mark_delete_pending = MagicMock()
    monkeypatch.setattr(
        "app.services.job_media_service.mark_delete_pending", mark_delete_pending,
    )

    replies = message_router._route_upload_conflict(
        IntentResult(intent="chitchat", structured_data={}),
        _message("继续"),
        MagicMock(role="factory"),
        session,
        db,
    )

    assert replies[0].content != message_router.CONFLICT_RESUME_FMT.format(
        field_name="需要的字段"
    )
    mark_delete_pending.assert_called_once_with(db, [41, 42])
    assert session.pending_upload_intent is None
    assert session.pending_upload_media_ids == []
    assert session.active_flow == "idle"


@pytest.mark.parametrize("expired_upload_draft", [False, True])
def test_image_cannot_extend_expired_first_publish_draft(
    monkeypatch, expired_upload_draft,
):
    session = _expired_session("upload_collecting")
    db = MagicMock()
    mark_delete_pending = MagicMock()
    save_session = MagicMock()
    monkeypatch.setattr(
        message_router.conversation_service, "load_session", lambda _userid: session,
    )
    monkeypatch.setattr(
        message_router.conversation_service, "save_session", save_session,
    )
    monkeypatch.setattr(
        "app.services.job_media_service.mark_delete_pending", mark_delete_pending,
    )

    replies = message_router._handle_image(
        _image_message(expired_upload_draft=expired_upload_draft),
        MagicMock(role="factory"),
        db,
    )

    assert replies[0].content == message_router.PENDING_EXPIRED_REPLY
    mark_delete_pending.assert_called_once_with(db, [41, 42])
    save_session.assert_called_once_with("factory-expired-upload", session)
    assert session.pending_upload == {}
    assert session.pending_upload_intent is None
    assert session.pending_upload_media_ids == []
    assert session.active_flow == "idle"


def test_queued_image_after_expiry_cannot_fall_back_to_old_job(monkeypatch):
    session = _expired_session("upload_collecting")
    session.current_intent = "upload_job"
    attach_image = MagicMock(return_value="should not attach")
    save_session = MagicMock()
    monkeypatch.setattr(
        message_router.conversation_service, "load_session", lambda _userid: session,
    )
    monkeypatch.setattr(
        message_router.conversation_service, "save_session", save_session,
    )
    monkeypatch.setattr(message_router.upload_service, "attach_image", attach_image)
    monkeypatch.setattr(
        "app.services.job_media_service.mark_delete_pending", MagicMock(),
    )

    first = message_router._handle_image(
        _image_message(), MagicMock(role="factory"), MagicMock(),
    )
    second = message_router._handle_image(
        WeComMessage(
            msg_id="queued-after-expiry",
            from_user="factory-expired-upload",
            msg_type="image",
            media_id="queued-media",
            image_url="images/queued-after-expiry.jpg",
        ),
        MagicMock(role="factory"),
        MagicMock(),
    )

    assert first[0].content == message_router.PENDING_EXPIRED_REPLY
    assert second[0].content == message_router.IMAGE_RECEIVED_NON_UPLOAD
    attach_image.assert_not_called()
    assert session.current_intent == "upload_job"
    assert session.attachment_target_id is None
