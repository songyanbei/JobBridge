from types import SimpleNamespace
from unittest.mock import patch

from app.llm.base import IntentResult
from app.schemas.search import SearchOutcome
from app.services import message_router
from app.services import conversation_service
from app.services.demo_message_context import DemoActorContext
from app.services.aibot_identity_gate import AibotIdentityResolution
from app.services.user_service import UserContext
from app.services.upload_service import UploadResult
from app.wecom.callback import WeComMessage


def test_router_handles_demo_command_without_llm_and_replies_to_real_actor():
    context = DemoActorContext(
        demo_mode=True,
        demo_id="demo-1",
        real_actor_userid="wecom-1",
        effective_userid="demo-factory-1",
        active_role="factory",
    )
    msg = WeComMessage(
        msg_id="demo-command-1", from_user="wecom-1", msg_type="text",
        content="/演示 厂家", source_channel="wecom_app",
    )
    with patch.object(
        message_router, "classify_intent",
        side_effect=AssertionError("LLM must not run"),
    ):
        replies = message_router.process(
            msg, SimpleNamespace(), demo_context=context,
            demo_command_reply="已进入【厂家】演示模式。",
        )
    assert len(replies) == 1
    assert replies[0].userid == "wecom-1"
    assert replies[0].content.startswith("已进入")


def _demo_turn_context(role: str) -> DemoActorContext:
    return DemoActorContext(
        demo_mode=True,
        demo_id="demo-1",
        real_actor_userid="real-actor-1",
        effective_userid=f"demo-{role}-1",
        active_role=role,
        conversation_type="single",
        conversation_id="real-actor-1",
    )


def _demo_user_context(context: DemoActorContext) -> UserContext:
    return UserContext(
        external_userid=context.effective_userid,
        role=context.active_role,
        status="active",
        display_name="演示用户",
        company="演示工厂" if context.active_role == "factory" else None,
        contact_person="演示用户" if context.active_role == "factory" else None,
        phone=None,
        can_search_jobs=context.active_role in {"worker", "broker"},
        can_search_workers=context.active_role in {"factory", "broker"},
        is_first_touch=False,
        should_welcome=False,
    )


def _stage_demo_session(monkeypatch, context: DemoActorContext):
    stored: dict[str, dict] = {}

    monkeypatch.setattr(
        conversation_service,
        "redis_get_session",
        lambda key: stored.get(key),
    )

    def save(key, payload, expected_version, **_kwargs):
        if expected_version != int((stored.get(key) or {}).get("session_version") or 0):
            return False
        stored[key] = payload
        return True

    monkeypatch.setattr(conversation_service, "redis_save_session_if_version", save)
    token = conversation_service.begin_session_staging(context.session_key)
    return stored, token


def _aibot_demo_message(content: str, *, msg_type: str = "text") -> WeComMessage:
    return WeComMessage(
        msg_id="demo-aibot-turn-1",
        from_user="real-actor-1",
        msg_type=msg_type,
        content=content,
        source_channel="wecom_aibot",
        actor_id_kind="opaque",
        conversation_type="single",
        conversation_id="real-actor-1",
    )


def _patch_demo_aibot_identity():
    return patch.object(
        message_router,
        "resolve_aibot_identity",
        return_value=AibotIdentityResolution(
            status="verified", mapped_external_userid="real-actor-1",
        ),
    )


def test_demo_aibot_worker_search_uses_staged_demo_session_key(monkeypatch):
    """A real AIBot demo search must not stage under the synthetic principal."""
    context = _demo_turn_context("worker")
    user_context = _demo_user_context(context)
    monkeypatch.setattr(message_router.user_service, "update_last_active", lambda *_args: None)
    _stored, token = _stage_demo_session(monkeypatch, context)
    try:
        monkeypatch.setattr(
            message_router,
            "classify_intent",
            lambda **_kwargs: IntentResult(
                intent="search_job",
                structured_data={"city": ["苏州市"], "job_category": ["电子厂"]},
                missing_fields=[],
                confidence=1.0,
            ),
        )
        monkeypatch.setattr(
            message_router,
            "_run_search",
            lambda *_args, **_kwargs: (
                SimpleNamespace(reply_text="找到岗位", result_count=1),
                SearchOutcome(
                    direction="search_job", criteria_used={
                        "city": ["苏州市"], "job_category": ["电子厂"],
                    }, initial_count=1, final_count=1, desired_count=3,
                    low_recall_threshold=3,
                ),
            ),
        )
        monkeypatch.setattr(
            message_router,
            "_post_search_dispatch",
            lambda **kwargs: [message_router._reply(kwargs["msg"].from_user, "找到岗位")],
        )
        with _patch_demo_aibot_identity():
            replies = message_router.process(
                _aibot_demo_message("我想找苏州电子厂"),
                SimpleNamespace(),
                user_context=user_context,
                demo_context=context,
            )
        assert replies[0].userid == context.real_actor_userid
        assert replies[0].content == "找到岗位"
    finally:
        staged = conversation_service.end_session_staging(token)
    assert staged is not None
    assert staged.userid == context.session_key


def test_demo_aibot_factory_upload_uses_staged_demo_session_key(monkeypatch):
    """Factory publishing/upload must share the same isolated staged session."""
    context = _demo_turn_context("factory")
    user_context = _demo_user_context(context)
    monkeypatch.setattr(message_router.user_service, "update_last_active", lambda *_args: None)
    _stored, token = _stage_demo_session(monkeypatch, context)
    try:
        monkeypatch.setattr(
            message_router,
            "classify_intent",
            lambda **_kwargs: IntentResult(
                intent="upload_job",
                structured_data={
                    "city": ["苏州市"], "job_category": ["电子厂"],
                    "title": "普工", "headcount": 10,
                    "salary_min": 5000, "salary_max": 7000,
                },
                missing_fields=[],
                confidence=1.0,
            ),
        )
        monkeypatch.setattr(
            "app.services.permission_service.can_publish_job",
            lambda _user_ctx: True,
        )
        monkeypatch.setattr(
            message_router.upload_service,
            "process_upload",
            lambda **_kwargs: UploadResult(True, "岗位已提交"),
        )
        with _patch_demo_aibot_identity():
            replies = message_router.process(
                _aibot_demo_message("苏州电子厂招普工", msg_type="text"),
                SimpleNamespace(),
                user_context=user_context,
                demo_context=context,
            )
        assert replies[0].userid == context.real_actor_userid
        assert replies[0].content == "岗位已提交"
    finally:
        staged = conversation_service.end_session_staging(token)
    assert staged is not None
    assert staged.userid == context.session_key
