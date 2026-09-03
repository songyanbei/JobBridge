import pytest

from app.wecom.aibot_client import AibotClient, AibotClientError


def test_official_subscribe_fixture_and_secret_only_in_body():
    client = AibotClient("BOTID", "SECRET")
    frame = client.subscribe("sub-1")
    assert frame == {"cmd": "aibot_subscribe", "headers": {"req_id": "sub-1"}, "body": {"bot_id": "BOTID", "secret": "SECRET"}}


def test_stream_req_id_and_finish_constraints():
    client = AibotClient("BOTID", "SECRET")
    first = client.stream("msg-1", "stream-1", "part")
    assert first["body"]["stream"]["finish"] is False
    final = client.stream("msg-1", "stream-1", "done", finish=True)
    assert final["cmd"] == "aibot_respond_msg"
    with pytest.raises(AibotClientError):
        client.stream("msg-1", "stream-1", "again")


def test_template_card_update_uses_official_response_shape():
    client = AibotClient("BOTID", "SECRET")
    frame = client.respond_update_msg("evt-1", {"card_type": "button_interaction"})
    assert frame == {
        "cmd": "aibot_respond_update_msg",
        "headers": {"req_id": "evt-1"},
        "body": {
            "response_type": "update_template_card",
            "template_card": {"card_type": "button_interaction"},
        },
    }
