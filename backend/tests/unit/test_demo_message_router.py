from types import SimpleNamespace
from unittest.mock import patch

from app.services import message_router
from app.services.demo_message_context import DemoActorContext
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
