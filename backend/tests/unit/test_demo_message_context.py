from types import SimpleNamespace
from unittest.mock import patch

from app.services.demo_message_context import (
    DEMO_ACTIVE_PREFIX,
    DemoActorContext,
    DemoCommandResult,
    clear_active_context,
    load_active_context,
    parse_demo_command,
    resolve_active_context,
    save_active_context,
)


class FakeRedis:
    def __init__(self):
        self.values = {}

    def setex(self, key, ttl, value):
        self.values[key] = value

    def get(self, key):
        return self.values.get(key)

    def delete(self, key):
        self.values.pop(key, None)


def test_parse_demo_commands_is_exact_and_deterministic():
    assert parse_demo_command("/演示 厂家") == ("activate", "factory")
    assert parse_demo_command("/演示 中介") == ("activate", "broker")
    assert parse_demo_command("/退出演示") == ("exit", None)
    assert parse_demo_command("我想演示厂家流程") is None
    assert parse_demo_command("/演示 厂家并搜索") == ("help", None)


def test_demo_context_separates_reply_and_business_ids():
    context = DemoActorContext(
        demo_mode=True,
        demo_id="demo-1",
        real_actor_userid="wecom-1",
        effective_userid="demo-factory-1",
        active_role="factory",
        conversation_type="single",
        conversation_id="wecom-1",
    )
    assert context.reply_userid == "wecom-1"
    assert context.session_key == "demo:session:demo-1:single:wecom-1:factory"
    assert context.effective_userid != context.reply_userid


def test_active_pointer_round_trip_and_clear():
    redis = FakeRedis()
    context = DemoActorContext(
        demo_mode=True,
        demo_id="demo-1",
        real_actor_userid="wecom-1",
        effective_userid="demo-worker-1",
        active_role="worker",
    )
    with patch("app.services.demo_message_context.get_redis", return_value=redis):
        save_active_context(context)
        loaded = load_active_context("wecom-1", conversation_id="chat-1")
        assert loaded is not None
        assert loaded.conversation_id == "chat-1"
        assert loaded.session_key.endswith(":worker")
        clear_active_context("wecom-1")
        assert load_active_context("wecom-1") is None


def test_disabled_or_missing_pointer_fails_closed():
    redis = FakeRedis()
    with patch("app.services.demo_message_context.get_redis", return_value=redis), patch(
        "app.services.demo_message_context._setting", side_effect=lambda name, default=None: {
             "app_env": "development", "demo_mode_enabled": True,
             "demo_allowed_bot_ids": "bot-1", "demo_session_ttl_seconds": 1800,
         }.get(name, default),
    ):
        assert resolve_active_context(
            db=SimpleNamespace(), real_actor_userid="wecom-1", bot_id="bot-1",
        ) is None
